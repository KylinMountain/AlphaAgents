"""Deterministic price/volume regime classifier + supply-warning validators.

This module provides regime classification independent of LLM. The system
previously overloaded ``llm_phase`` to serve two roles — (a) Wyckoff
structural state (吸筹/拉升/派发/下跌) and (b) daily trend label — and the
guard layer enforced phase continuity, causing the phase tag to lag the
actual price action by weeks (e.g. 300443 held "下跌初期" through a +20%
recovery; 301379 held "拉升初期" through a -6% multi-day downtrend).

The fix is to NOT ask the LLM for daily trend. Daily trend is a
mechanical question — moving averages, recent returns, OBV — and is
computed here as ``price_regime`` based on settled bars only.

Regime categories (return value of ``classify_regime``):

* ``uptrend``     — MA5>MA10>MA20, 10-day return ≥ +3%
* ``uptrend_weak`` — MA5>MA10 but MA10<MA20 or 10d return modest (+0–3%)
* ``downtrend``   — MA5<MA10<MA20, 10-day return ≤ -3%
* ``downtrend_weak`` — MA5<MA10 but mixed signals on MA20 / return
* ``range``       — MA spread <2% of price, |10d_ret| < 3%, normal vol
* ``reversal_attempt`` — MA cross in the last 3 bars + volume spike

Supply-warning validators (``detect_supply_warnings``) target patterns
the Anna prompt under-weights when the LLM has already locked phase:

* ``failed_sos_followthrough`` — SOS day (D-1 or D-2) was big-up + high
  volume; today opens above prior close but closes in the lower half
  with elevated volume → effort without result, supply absorbed demand.
* ``upthrust`` — single-day spike with long upper shadow (>40% of
  range), close in lower half, volume ≥1.5x 20-day median.
* ``supply_on_rally`` — green bar but close in lower half + volume ≥
  1.5x 20-day median; pricing structure is up but participation is
  rejecting the highs.
* ``bearish_engulfing`` — today's red body wholly engulfs yesterday's
  green body with high volume.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd


# Thresholds tuned for daily A-share OHLCV. Conservative — designed to
# under-fire rather than over-fire so the regime stays trustworthy.
UPTREND_RETURN_PCT = 3.0
DOWNTREND_RETURN_PCT = -3.0
RANGE_MA_SPREAD_PCT = 2.0
RANGE_MAX_ABS_RETURN_PCT = 3.0
VOL_SPIKE_X = 1.5
UPPER_SHADOW_FRAC = 0.4

# Setup detection thresholds. Tuned to fire on breakout day, not the day
# after — the whole point of trade_setup is to decouple from Anna's
# "wait for confirmation" gate.
SETUP_BREAKOUT_LOOKBACK = 20
SETUP_VOL_X = 1.5
SETUP_CLOSE_IN_UPPER_HALF = 0.5  # close ≥ this fraction of (H-L) above L
SETUP_LARGE_MOVE_PCT = 4.0       # |day pct change| ≥ this counts as decisive
SETUP_CONTINUATION_VOL_X = 1.1
SETUP_MAX_CLOSE_VS_MA20_CONTINUATION_PCT = 8.0
LONG_ENTRY_SUPPLY_VETO = {
    "failed_sos_followthrough",
    "upthrust",
    "supply_on_rally",
    "bearish_engulfing",
}

VALID_TRADE_SETUPS = (
    "none",
    "long_watch", "long_actionable", "long_confirmed",
    "short_watch", "short_actionable", "short_confirmed",
)


def _safe_pct(numer: float, denom: float) -> float:
    if denom == 0 or pd.isna(denom):
        return 0.0
    return (numer - denom) / denom * 100.0


def _ma(closes: pd.Series, window: int) -> float:
    if len(closes) < window:
        return float("nan")
    return float(closes.iloc[-window:].mean())


def compute_ma_structure(df: pd.DataFrame, *, as_of: Optional[str] = None) -> dict:
    """All MA-derived trend fields the LLM, screener, and chart need.

    Anna VPA itself doesn't depend on MAs, but a quantitative system
    using LLM judgement needs a deterministic trend guardrail. Without
    it the LLM (or our climax-scoring screener) can confidently label a
    multi-week downtrend as "拉升初期" because the volume narrative
    locally supports the prior phase. The reviewer audit (2026-05-11)
    found ``301379`` held "拉升初期" through MA5<MA10<MA20 with a -10%
    10-day return — exactly the failure mode MA fields prevent.

    Returns ``{ma5, ma10, ma20, ma60, ma_stack, ma20_slope_5d,
                close_vs_ma20_pct, close_vs_ma60_pct, as_of}``.

    * ``ma_stack`` ∈ ``{bullish, bearish, mixed, insufficient}`` where
      bullish = MA5>MA10>MA20, bearish = MA5<MA10<MA20.
    * ``ma20_slope_5d`` = pct change of MA20 over the last 5 bars; a
      positive value means MA20 is trending up.
    * ``close_vs_ma*_pct`` measures extension (% of MA the close is at);
      high values flag追高 risk, very negative values flag oversold.
    """
    out: dict = {"as_of": None, "ma5": None, "ma10": None, "ma20": None,
                 "ma60": None, "ma_stack": "insufficient",
                 "ma20_slope_5d": None,
                 "close_vs_ma20_pct": None, "close_vs_ma60_pct": None}
    if df is None or len(df) < 20:
        return out
    df = df.copy()
    if as_of:
        df = df[df["date"].astype(str) <= as_of].reset_index(drop=True)
        if len(df) < 20:
            return out
    closes = df["close"].astype(float)
    last_close = float(closes.iloc[-1])
    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60) if len(closes) >= 60 else None

    # MA stack — full bullish / full bearish / anything else
    if pd.notna(ma5) and pd.notna(ma10) and pd.notna(ma20):
        if ma5 > ma10 > ma20:
            stack = "bullish"
        elif ma5 < ma10 < ma20:
            stack = "bearish"
        else:
            stack = "mixed"
    else:
        stack = "insufficient"

    # 5-bar slope of MA20 (positive = trending up). Need 25 bars total
    # to compute (20 for MA20 today + 5 lookback for the prior MA20).
    slope = None
    if len(closes) >= 25:
        ma20_5d_ago = float(closes.iloc[-25:-5].mean())
        slope = _safe_pct(ma20, ma20_5d_ago)

    out.update({
        "as_of": str(df["date"].iloc[-1]),
        "ma5": round(ma5, 3) if pd.notna(ma5) else None,
        "ma10": round(ma10, 3) if pd.notna(ma10) else None,
        "ma20": round(ma20, 3) if pd.notna(ma20) else None,
        "ma60": round(ma60, 3) if (ma60 is not None and pd.notna(ma60)) else None,
        "ma_stack": stack,
        "ma20_slope_5d": round(slope, 2) if slope is not None else None,
        "close_vs_ma20_pct": round(_safe_pct(last_close, ma20), 2) if pd.notna(ma20) else None,
        "close_vs_ma60_pct": round(_safe_pct(last_close, ma60), 2) if (ma60 is not None and pd.notna(ma60)) else None,
    })
    return out


def classify_regime(df: pd.DataFrame, *, as_of: Optional[str] = None) -> dict:
    """Classify the daily price/volume regime for the latest bar in ``df``.

    Args:
      df: OHLCV DataFrame with columns date/open/high/low/close/volume.
          Must be sorted ascending by date.
      as_of: Optional YYYY-MM-DD; if set, classification is for that
          date (it must be present in df). If unset, uses the last row.

    Returns:
      ``{"regime": str, "ma5": float, "ma10": float, "ma20": float,
         "ret_10d": float, "ret_20d": float, "vol_x_20d": float,
         "as_of": str, "reason": str}``.
      ``regime`` is one of uptrend / uptrend_weak / downtrend /
      downtrend_weak / range / reversal_attempt / insufficient_data.
    """
    if df is None or len(df) < 21:
        return {"regime": "insufficient_data", "reason": f"need ≥21 bars, got {len(df) if df is not None else 0}"}

    df = df.copy()
    if as_of:
        if as_of not in set(df["date"].astype(str)):
            return {"regime": "insufficient_data", "reason": f"as_of={as_of} not in df"}
        df = df[df["date"].astype(str) <= as_of].reset_index(drop=True)
        if len(df) < 21:
            return {"regime": "insufficient_data", "reason": f"only {len(df)} bars through {as_of}"}

    closes = df["close"].astype(float)
    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    last_close = float(closes.iloc[-1])
    close_10d_ago = float(closes.iloc[-11]) if len(closes) >= 11 else last_close
    close_20d_ago = float(closes.iloc[-21]) if len(closes) >= 21 else last_close
    ret_10d = _safe_pct(last_close, close_10d_ago)
    ret_20d = _safe_pct(last_close, close_20d_ago)

    vols = df["volume"].astype(float)
    vol_20d_median = float(vols.iloc[-21:-1].median()) if len(vols) >= 21 else float(vols.median())
    last_vol = float(vols.iloc[-1])
    vol_x = last_vol / vol_20d_median if vol_20d_median > 0 else 1.0

    # MA spread (% of last close) — small spread is a range signature.
    if ma5 and ma10 and ma20 and last_close > 0:
        ma_spread_pct = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / last_close * 100
    else:
        ma_spread_pct = float("nan")

    out = {
        "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "ret_10d": ret_10d, "ret_20d": ret_20d,
        "vol_x_20d": vol_x, "ma_spread_pct": ma_spread_pct,
        "as_of": str(df["date"].iloc[-1]),
    }

    # Reversal attempt: MA5 crossed MA10 in last 3 bars + volume spike today.
    cross_recent = False
    if len(closes) >= 13:
        ma5_today = _ma(closes, 5)
        ma10_today = _ma(closes, 10)
        ma5_3d_ago = float(closes.iloc[-8:-3].mean())
        ma10_3d_ago = float(closes.iloc[-13:-3].mean())
        # cross direction reversed between 3d-ago and today
        sign_today = 1 if ma5_today > ma10_today else (-1 if ma5_today < ma10_today else 0)
        sign_3d = 1 if ma5_3d_ago > ma10_3d_ago else (-1 if ma5_3d_ago < ma10_3d_ago else 0)
        cross_recent = sign_today != sign_3d and sign_today != 0
    if cross_recent and vol_x >= VOL_SPIKE_X:
        out["regime"] = "reversal_attempt"
        out["reason"] = f"MA5/MA10 cross within 3 bars + vol_x={vol_x:.2f}"
        return out

    # Range: MAs converged + small recent return — pre-breakout consolidation.
    if (
        ma_spread_pct == ma_spread_pct  # not NaN
        and ma_spread_pct < RANGE_MA_SPREAD_PCT
        and abs(ret_10d) < RANGE_MAX_ABS_RETURN_PCT
    ):
        out["regime"] = "range"
        out["reason"] = f"MA spread {ma_spread_pct:.2f}% < {RANGE_MA_SPREAD_PCT}%, |ret_10d|={abs(ret_10d):.2f}%"
        return out

    # Uptrend: stacked MAs + meaningful gain.
    if ma5 > ma10 > ma20 and ret_10d >= UPTREND_RETURN_PCT:
        out["regime"] = "uptrend"
        out["reason"] = f"MA5>MA10>MA20, ret_10d={ret_10d:+.2f}%"
        return out
    if ma5 > ma10 and ret_10d >= 0:
        out["regime"] = "uptrend_weak"
        out["reason"] = f"MA5>MA10 only, ret_10d={ret_10d:+.2f}%"
        return out

    # Downtrend: stacked MAs + meaningful loss.
    if ma5 < ma10 < ma20 and ret_10d <= DOWNTREND_RETURN_PCT:
        out["regime"] = "downtrend"
        out["reason"] = f"MA5<MA10<MA20, ret_10d={ret_10d:+.2f}%"
        return out
    if ma5 < ma10 and ret_10d <= 0:
        out["regime"] = "downtrend_weak"
        out["reason"] = f"MA5<MA10 only, ret_10d={ret_10d:+.2f}%"
        return out

    # Fallback: mixed signals — classify by 10d return sign.
    out["regime"] = "uptrend_weak" if ret_10d > 0 else "downtrend_weak"
    out["reason"] = f"mixed MAs, ret_10d={ret_10d:+.2f}%"
    return out


def detect_supply_warnings(df: pd.DataFrame, *, as_of: Optional[str] = None) -> list[dict]:
    """Detect supply-on-rally patterns on the latest bar.

    Returns a list of warning dicts; empty list = no warnings.
    Each entry: ``{"pattern": str, "label": str, "detail": str}``.
    """
    if df is None or len(df) < 21:
        return []
    df = df.copy()
    if as_of:
        if as_of not in set(df["date"].astype(str)):
            return []
        df = df[df["date"].astype(str) <= as_of].reset_index(drop=True)
        if len(df) < 3:
            return []

    today = df.iloc[-1]
    yest = df.iloc[-2]
    o, h, l, c, v = float(today["open"]), float(today["high"]), float(today["low"]), float(today["close"]), float(today["volume"])
    yo, yh, yl, yc, yv = float(yest["open"]), float(yest["high"]), float(yest["low"]), float(yest["close"]), float(yest["volume"])

    vols = df["volume"].astype(float)
    vol_med_20d = float(vols.iloc[-21:-1].median()) if len(vols) >= 21 else float(vols.median())
    vol_x = v / vol_med_20d if vol_med_20d > 0 else 1.0

    rng = max(h - l, 1e-9)
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l
    body = abs(c - o)
    close_in_range = (c - l) / rng  # 0 = at low, 1 = at high

    warnings: list[dict] = []

    # ── 1. failed_sos_followthrough ──────────────────────────────
    # Look back 2 bars for an SOS-like big-up + high-vol day. If today
    # opens above yesterday's close (gap-up) but closes in lower half
    # with vol ≥ 1.2x, the demand burst didn't follow through.
    for lookback in (1, 2):
        if len(df) < lookback + 2:
            continue
        sos_bar = df.iloc[-1 - lookback]
        sos_pct = _safe_pct(float(sos_bar["close"]), float(df.iloc[-2 - lookback]["close"])) if len(df) >= lookback + 2 else 0.0
        sos_vol_x = float(sos_bar["volume"]) / vol_med_20d if vol_med_20d > 0 else 1.0
        if sos_pct >= 5.0 and sos_vol_x >= 2.0:
            # SOS candidate detected; check today's follow-through quality
            gap_up = o > yc
            if gap_up and close_in_range < 0.5 and vol_x >= 1.2:
                warnings.append({
                    "pattern": "failed_sos_followthrough",
                    "label": "SOS 跟进失败",
                    "detail": (
                        f"前{lookback}日 SOS 候选 (涨{sos_pct:+.1f}%, vol_x={sos_vol_x:.1f}); "
                        f"今高开 close 在区间下半 ({close_in_range:.2f}), vol_x={vol_x:.1f}"
                    ),
                })
            break

    # ── 2. upthrust ─────────────────────────────────────────────
    # Long upper shadow + close in lower half + high volume.
    if upper_shadow / rng >= UPPER_SHADOW_FRAC and close_in_range < 0.5 and vol_x >= VOL_SPIKE_X:
        warnings.append({
            "pattern": "upthrust",
            "label": "上影 upthrust",
            "detail": f"上影={upper_shadow/rng:.0%} 区间, close_in_range={close_in_range:.2f}, vol_x={vol_x:.1f}",
        })

    # ── 3. supply_on_rally ──────────────────────────────────────
    # Green bar (c > o) but close in lower half + high vol. Effort up
    # but result mediocre — typical Wyckoff supply test.
    if c > o and close_in_range < 0.5 and vol_x >= VOL_SPIKE_X and (h - max(o, c)) > body:
        warnings.append({
            "pattern": "supply_on_rally",
            "label": "上涨现供应",
            "detail": f"阳线 close_in_range={close_in_range:.2f}, 上影>实体, vol_x={vol_x:.1f}",
        })

    # ── 4. bearish_engulfing ────────────────────────────────────
    # Red body wholly engulfs prior green body with high volume.
    yest_green = yc > yo
    today_red = c < o
    engulfs = o >= yc and c <= yo
    if yest_green and today_red and engulfs and vol_x >= 1.2:
        warnings.append({
            "pattern": "bearish_engulfing",
            "label": "看跌吞没",
            "detail": f"前阳后阴吞没 (前 {yo:.2f}-{yc:.2f} → 今 {o:.2f}-{c:.2f}), vol_x={vol_x:.1f}",
        })

    return warnings


def _recent_supply_warning_patterns(df: pd.DataFrame, lookback: int = 2) -> set[str]:
    """Supply warnings on the current/prior settled bars.

    Entry vetoes need to know whether the previous bar already showed supply.
    Running the detector on each trailing prefix keeps the calculation
    point-in-time and avoids looking at future bars.
    """
    if df is None or len(df) < 20:
        return set()
    pats: set[str] = set()
    n = min(lookback, len(df))
    for offset in range(n):
        sub = df.iloc[: len(df) - offset]
        for warning in detect_supply_warnings(sub):
            pattern = warning.get("pattern")
            if pattern:
                pats.add(str(pattern))
    return pats


def derive_trade_setup(df: pd.DataFrame, *, as_of: Optional[str] = None) -> dict:
    """Deterministic trade-setup inference for the latest bar.

    This is the **trade-trigger** field — independent of ``llm_phase`` and
    ``llm_confirmation_level``. While Anna's ``confirmation_level`` is a
    *Wyckoff event confidence* score (requires forward bars to confirm),
    ``trade_setup`` is a *current-bar action* score: can I act today?

    The two answer different questions and need different thresholds. The
    old design conflated them under one knob (``level≥2 → BUY``) and that
    bias made the system structurally late on bullish entries while still
    catching bearish breaks quickly.

    Returns ``{"setup": str, "score": float, "reason": str, "features": dict}``
    where ``setup`` ∈ ``VALID_TRADE_SETUPS``.
    """
    if df is None or len(df) < SETUP_BREAKOUT_LOOKBACK + 2:
        return {"setup": "none", "score": 0.0, "reason": "insufficient_history", "features": {}}

    df = df.copy()
    if as_of:
        df = df[df["date"].astype(str) <= as_of].reset_index(drop=True)
        if len(df) < SETUP_BREAKOUT_LOOKBACK + 2:
            return {"setup": "none", "score": 0.0, "reason": "insufficient_history_at_as_of", "features": {}}

    today = df.iloc[-1]
    yest = df.iloc[-2]
    o, h, l, c, v = float(today["open"]), float(today["high"]), float(today["low"]), float(today["close"]), float(today["volume"])
    yc = float(yest["close"])
    rng = max(h - l, 1e-9)
    close_in_range = (c - l) / rng
    body_to_range = abs(c - o) / rng
    pct_change = _safe_pct(c, yc)

    # 20-day prior high/low (not including today)
    prior_20 = df.iloc[-1 - SETUP_BREAKOUT_LOOKBACK : -1]
    prior_high = float(prior_20["high"].max())
    prior_low = float(prior_20["low"].min())

    # Volume regime
    vols = df["volume"].astype(float)
    vol_med_20d = float(vols.iloc[-21:-1].median()) if len(vols) >= 21 else float(vols.median())
    vol_x = v / vol_med_20d if vol_med_20d > 0 else 1.0

    # Daily regime + supply warnings (reuse existing fns)
    regime_info = classify_regime(df)
    regime = regime_info.get("regime", "unknown")
    warnings = detect_supply_warnings(df)
    warning_pats = {w["pattern"] for w in warnings}
    recent_warning_pats = _recent_supply_warning_patterns(df, lookback=2)
    ma_info = compute_ma_structure(df)
    close_vs_ma20 = ma_info.get("close_vs_ma20_pct")

    # Mini-streak features (used for *_confirmed escalation)
    closes = df["close"].astype(float).tolist()
    higher_lows_streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if float(df.iloc[i]["low"]) >= float(df.iloc[i - 1]["low"]):
            higher_lows_streak += 1
        else:
            break
    lower_highs_streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if float(df.iloc[i]["high"]) <= float(df.iloc[i - 1]["high"]):
            lower_highs_streak += 1
        else:
            break

    features = {
        "regime": regime, "pct_change": round(pct_change, 2),
        "vol_x": round(vol_x, 2), "close_in_range": round(close_in_range, 2),
        "body_to_range": round(body_to_range, 2),
        "prior_high": round(prior_high, 3), "prior_low": round(prior_low, 3),
        "today_close": round(c, 3),
        "broke_prior_high": c > prior_high,
        "broke_prior_low": c < prior_low,
        "warnings": list(warning_pats),
        "recent_supply_warnings": sorted(recent_warning_pats),
        "higher_lows_streak": higher_lows_streak,
        "lower_highs_streak": lower_highs_streak,
        "close_vs_ma20_pct": round(close_vs_ma20, 2) if close_vs_ma20 is not None else None,
        "ma_stack": ma_info.get("ma_stack"),
    }

    # ── Short side first (warnings short-circuit any long setup) ────
    is_break_down = c < prior_low and pct_change <= -SETUP_LARGE_MOVE_PCT / 2
    short_active_warning = warning_pats & {"failed_sos_followthrough", "upthrust", "supply_on_rally", "bearish_engulfing"}

    if is_break_down and vol_x >= SETUP_VOL_X and close_in_range <= SETUP_CLOSE_IN_UPPER_HALF:
        if lower_highs_streak >= 3 and regime in ("downtrend", "downtrend_weak"):
            return {"setup": "short_confirmed", "score": 0.9,
                    "reason": f"broke prior_low={prior_low:.2f} on vol_x={vol_x:.1f}, "
                              f"close_in_range={close_in_range:.2f}, streak={lower_highs_streak}d",
                    "features": features}
        return {"setup": "short_actionable", "score": 0.7,
                "reason": f"broke prior_low={prior_low:.2f} on vol_x={vol_x:.1f}, close_in_range={close_in_range:.2f}",
                "features": features}

    if short_active_warning and regime in ("downtrend", "downtrend_weak", "reversal_attempt"):
        return {"setup": "short_watch", "score": 0.5,
                "reason": f"supply warning {sorted(short_active_warning)} in {regime}; risk flag, not structural short",
                "features": features}
    if short_active_warning and regime in ("uptrend_weak", "range"):
        return {"setup": "short_watch", "score": 0.4,
                "reason": f"supply warning {sorted(short_active_warning)} in {regime} (top-side test)",
                "features": features}

    # ── Long side ─────────────────────────────────────────────
    long_veto = recent_warning_pats & LONG_ENTRY_SUPPLY_VETO
    if long_veto:
        return {"setup": "none", "score": 0.0,
                "reason": f"long veto: recent supply warning {sorted(long_veto)}",
                "features": features}

    is_break_up = c > prior_high and pct_change >= SETUP_LARGE_MOVE_PCT
    is_strong_close = close_in_range >= SETUP_CLOSE_IN_UPPER_HALF and vol_x >= SETUP_VOL_X

    if is_break_up and vol_x >= SETUP_VOL_X and is_strong_close:
        if regime in ("uptrend",) and higher_lows_streak >= 3:
            return {"setup": "long_confirmed", "score": 0.9,
                    "reason": f"broke prior_high={prior_high:.2f} on vol_x={vol_x:.1f}, "
                              f"close_in_range={close_in_range:.2f}, HL streak={higher_lows_streak}d",
                    "features": features}
        if regime in ("uptrend", "uptrend_weak", "reversal_attempt", "range"):
            return {"setup": "long_actionable", "score": 0.7,
                    "reason": f"broke prior_high={prior_high:.2f} on vol_x={vol_x:.1f}, "
                              f"close_in_range={close_in_range:.2f}, regime={regime}",
                    "features": features}

    # Continuation: already in uptrend, today is a healthy follow-through bar.
    # Keep this narrower than breakout logic: otherwise it becomes a chase-high
    # trigger after several extended bars.
    extension_ok = (
        close_vs_ma20 is None
        or pd.isna(close_vs_ma20)
        or close_vs_ma20 <= SETUP_MAX_CLOSE_VS_MA20_CONTINUATION_PCT
    )
    if (
        regime == "uptrend"
        and c > yc
        and pct_change >= 1.0
        and close_in_range >= 0.6
        and vol_x >= SETUP_CONTINUATION_VOL_X
        and higher_lows_streak >= 3
        and extension_ok
    ):
        close_vs_label = "n/a" if close_vs_ma20 is None or pd.isna(close_vs_ma20) else f"{close_vs_ma20:+.1f}%"
        return {"setup": "long_confirmed", "score": 0.8,
                "reason": f"uptrend continuation, HL streak={higher_lows_streak}d, "
                          f"close strong, close_vs_ma20={close_vs_label}",
                "features": features}

    if (
        regime == "uptrend"
        and c >= prior_high * 0.97
        and c >= yc
        and close_in_range >= 0.5
        and vol_x >= 1.0
        and higher_lows_streak >= 3
        and extension_ok
        and ma_info.get("ma_stack") == "bullish"
    ):
        close_vs_label = "n/a" if close_vs_ma20 is None or pd.isna(close_vs_ma20) else f"{close_vs_ma20:+.1f}%"
        return {"setup": "long_watch", "score": 0.45,
                "reason": f"uptrend resumption watch, near prior_high={prior_high:.2f}, "
                          f"HL streak={higher_lows_streak}d, close_vs_ma20={close_vs_label}",
                "features": features}

    # Trend pullback / continuation entry: in an uptrend, today shows
    # high-vol strength (pct ≥ 4% + close in upper half) but didn't make
    # a new 20-day high — typical "ride the trend" entry. v91's winner
    # 002066 9-23 sits here: regime=uptrend, vol_x=2.3, pct=+5.6%,
    # close above prior_high*0.95 — actionable even without a fresh
    # breakout, because the trend itself is the edge.
    near_prior_high = c >= prior_high * 0.95
    # Threshold is intentionally lower than the breakout path's ±4% — the
    # continuation branch already requires uptrend regime + close in upper
    # half + within 5% of prior_high, so we don't need a 4% bar to call it
    # actionable. v91's winner 002066 9-23 was a 3.17% close-at-day's-high
    # bar with vol_x=2.3 right at the prior_high — exactly the pattern
    # this branch is meant to capture.
    if (
        regime in ("uptrend", "uptrend_weak")
        and pct_change >= 2.5
        and vol_x >= SETUP_VOL_X
        and close_in_range >= SETUP_CLOSE_IN_UPPER_HALF
        and near_prior_high
    ):
        return {"setup": "long_actionable", "score": 0.6,
                "reason": f"in-trend strength day pct={pct_change:+.1f}%, vol_x={vol_x:.1f}, "
                          f"close_in_range={close_in_range:.2f}, near prior_high={prior_high:.2f}",
                "features": features}

    # Watch-level long: spring-like behavior (test of support, low vol, long lower shadow)
    lower_shadow = (min(o, c) - l) / rng if rng > 0 else 0
    near_support = c < prior_low * 1.03  # within 3% of prior 20d low
    if near_support and lower_shadow >= 0.4 and vol_x <= 0.8 and regime in ("downtrend_weak", "range", "reversal_attempt"):
        return {"setup": "long_watch", "score": 0.4,
                "reason": f"near support {prior_low:.2f}, long lower shadow, low vol — spring candidate",
                "features": features}

    # Reversal attempt with up-cross + decent volume but not yet a breakout
    if regime == "reversal_attempt" and pct_change > 0 and vol_x >= 1.2:
        return {"setup": "long_watch", "score": 0.4,
                "reason": f"reversal_attempt regime + positive vol_x={vol_x:.1f}",
                "features": features}

    return {"setup": "none", "score": 0.0,
            "reason": f"no setup criteria met (regime={regime}, vol_x={vol_x:.1f})",
            "features": features}
