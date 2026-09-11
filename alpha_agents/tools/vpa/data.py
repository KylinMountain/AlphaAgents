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

logger = logging.getLogger(__name__)


def _daily_limit_pct(code: str, name: str = "") -> float:
    """Return the daily price-limit percentage for an A-share by board.

    Spec §3.1: main board ±10%, ChiNext / STAR market ±20%, ST stocks ±5%.
    Without this per-board logic, v5.x's blanket ±10% rule never fires on
    300xxx (ChiNext) — which is our primary test stock — silently shipping
    a non-functional limit-up filter.
    """
    code = (code or "").strip()
    name = (name or "").strip()
    if name.upper().startswith(("ST ", "*ST ", "ST", "*ST")):
        return 0.05
    if code.startswith("688"):
        return 0.20
    if code.startswith("30"):
        return 0.20
    if code.startswith(("60", "00")):
        return 0.10
    return 0.10  # safe default for unknown prefix


def _load_ohlcv(code: str, days: int = 60, include_realtime: bool = False,
                as_of: str | None = None) -> Optional[pd.DataFrame]:
    """Fetch settled OHLCV bars from market_history.db (fallback baostock).

    Always returns settled bars only. Realtime intraday bars are NEVER mixed
    in here — they would pollute rolling stats (vol_ma, percentile ranks)
    by adding a non-stationary partial-day observation. Use
    ``_fetch_realtime_bar`` and ``_append_realtime_with_derived`` after
    ``_compute_derived`` to graft the partial bar onto a settled
    distribution.

    The ``include_realtime`` parameter is kept for back-compat with callers
    that pass it; it is ignored.

    ``as_of`` accepts either ``"YYYY-MM-DD"`` (EOD — include that date's close)
    or ``"YYYY-MM-DD HH:MM"`` (point-in-time — if HH:MM is pre-close, only
    bars < as_of_date are returned; this is what morning / intraday replay
    wants since today's close hasn't happened yet).
    """
    del include_realtime
    if as_of:
        from alpha_agents.evolution.replay_mode import effective_eod_cut_date
        cut = effective_eod_cut_date(as_of) or as_of[:10]
    else:
        cut = None

    history = get_local_history(code, days=days, as_of=cut)
    if not history or len(history) < 25:
        if not as_of:
            history = get_stock_history(code, days=days)
    if not history or len(history) < 25:
        return None

    df = pd.DataFrame(history)
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            return None
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
    if len(df) < 25:
        return None

    # Staleness gate: when running in replay/backtest mode (as_of set), the
    # stock's most recent bar must equal the latest market-wide trading day
    # at or before `cut`. Catches suspended stocks — e.g. 002968 paused
    # 2025-09-15→2025-09-26 would otherwise pass a 2025-09-23 screener call
    # with stale 09-12 data and produce a misleading bullish verdict.
    if as_of and cut:
        latest_market_day = get_latest_trading_day_at_or_before(cut)
        if latest_market_day and str(df.iloc[-1]["date"]) < latest_market_day:
            return None

    return df


def _fetch_realtime_bar(code: str) -> Optional[dict]:
    """Pull today's partial intraday bar from Sina, or None."""
    try:
        from alpha_agents.data.market_data import get_realtime_quotes
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        rt = get_realtime_quotes([code])
        if not rt or code not in rt:
            return None
        q = rt[code]
        price = q.get("price", 0)
        if price <= 0:
            return None
        return {
            "date": today,
            "open": float(q.get("open", price)),
            "high": float(q.get("high", price)),
            "low": float(q.get("low", price)),
            "close": float(price),
            "volume": int(q.get("volume", 0)),
        }
    except Exception as e:
        logger.debug("Realtime bar for %s failed: %s", code, e)
        return None


def _append_realtime_with_derived(df_settled: pd.DataFrame, bar: dict) -> pd.DataFrame:
    """Append a partial intraday bar to a derived dataframe.

    The bar's derived columns (vol_ma, volume_ratio, bar_spread, close_position,
    shadows, pct_change, vp_harmony) are evaluated against the *settled*
    rolling stats — so the bar is observed against the right baseline rather
    than allowed to influence its own rolling means. Percentile ranks
    (vr_pct60, abspct_pct20, spread_pct20) are computed against the trailing
    settled distributions.
    """
    if df_settled is None or len(df_settled) == 0:
        return df_settled
    last = df_settled.iloc[-1]
    if str(last.get("date", "")) == str(bar.get("date", "")):
        return df_settled  # same date already settled — don't double-count

    # v7 §3.2: shift baseline by 1 so today's intraday bar is measured against
    # the rolling mean that EXCLUDES the latest settled bar. Otherwise an
    # extreme close yesterday inflates the baseline and today's normal volume
    # looks small.
    vol_ma_shifted = df_settled["vol_ma"].shift(1)
    settled_vol_ma = (
        float(vol_ma_shifted.iloc[-1])
        if pd.notna(vol_ma_shifted.iloc[-1])
        else (float(last.get("vol_ma")) if pd.notna(last.get("vol_ma")) else float("nan"))
    )
    settled_vol_ma5 = float(last.get("vol_ma5")) if pd.notna(last.get("vol_ma5")) else float("nan")
    last_close = float(last["close"])

    o = float(bar["open"]); h = float(bar["high"]); l = float(bar["low"])
    c = float(bar["close"]); v = float(bar["volume"])
    hl = max(h - l, 0.0)
    spread = hl / c if c > 0 else 0.0
    cp = ((c - l) / hl) if hl > 0 else 0.5
    upper = ((h - max(o, c)) / hl) if hl > 0 else 0.0
    lower = ((min(o, c) - l) / hl) if hl > 0 else 0.0
    bar_type = "阳线" if c > o else ("阴线" if c < o else "十字星")
    vol_ratio = (v / settled_vol_ma) if settled_vol_ma and settled_vol_ma > 0 else float("nan")
    pct = (c - last_close) / last_close if last_close > 0 else 0.0

    if pct > 0 and vol_ratio > 1.0:
        vph = "一致(涨+放量)"
    elif pct < 0 and vol_ratio > 1.0:
        vph = "背离(跌+放量)"
    elif pct > 0 and vol_ratio < 0.8:
        vph = "背离(涨+缩量)"
    elif pct < 0 and vol_ratio < 0.8:
        vph = "一致(跌+缩量)"
    else:
        vph = "中性"

    def _rank_against(value: float, series: pd.Series) -> float:
        s = series.dropna()
        if len(s) == 0 or pd.isna(value):
            return 0.5
        # Percentile rank: fraction of past values <= the new value.
        return float((s <= value).sum() / len(s))

    vr_pct60 = _rank_against(
        vol_ratio,
        df_settled["volume_ratio"].tail(60),
    ) if not pd.isna(vol_ratio) else 0.5
    abspct_pct20 = _rank_against(
        abs(pct),
        df_settled["pct_change"].abs().tail(20),
    )
    spread_pct20 = _rank_against(
        spread,
        df_settled["bar_spread"].tail(20),
    )

    # v7 review C#2: realtime bar must carry the same v7 columns the
    # candidate scanner / cross-family validator read on settled bars,
    # otherwise live mode validation fails spuriously.
    #
    # range_vs_5d_avg: today's bar-range vs mean of prior 5 settled bars'
    # ranges (replaces validator's hardcoded 1.5 floor with a per-stock
    # 60d 90th-percentile cutoff in _phase_guard_context_from_df).
    prior_5d_ranges = (df_settled["high"] - df_settled["low"]).tail(5)
    prior_5d_mean_range = float(prior_5d_ranges.mean()) if len(prior_5d_ranges) >= 3 else float("nan")
    range_today = h - l
    range_vs_5d_avg = (
        range_today / prior_5d_mean_range
        if prior_5d_mean_range and prior_5d_mean_range > 0 and not pd.isna(prior_5d_mean_range)
        else float("nan")
    )

    # is_polluting_bar: limit-up/down detection for the realtime bar (so the
    # candidate scanner's polluting-fallback logic in §3.1 can reach today).
    # Pull code/name from df_settled when those columns exist (single-stock
    # df is the common case); otherwise default to the safe 10% main-board
    # limit via _daily_limit_pct("", "").
    rt_code = ""
    rt_name = ""
    if "code" in df_settled.columns:
        rt_code = str(df_settled["code"].iloc[-1])
    if "name" in df_settled.columns:
        rt_name = str(df_settled["name"].iloc[-1])
    rt_limit_pct = _daily_limit_pct(rt_code, rt_name)
    near_limit = abs(pct) >= 0.95 * rt_limit_pct
    no_intraday = spread < 0.005
    one_letter = spread < 0.001
    is_polluting = bool(one_letter or (near_limit and no_intraday))

    new_row = {
        "date": bar["date"], "open": o, "high": h, "low": l, "close": c, "volume": v,
        "vol_ma": settled_vol_ma, "volume_ratio": vol_ratio,
        "bar_spread": spread, "close_position": cp, "bar_type": bar_type,
        "upper_shadow": upper, "lower_shadow": lower,
        "pct_change": pct, "vol_ma5": settled_vol_ma5,
        "vol_trend_ratio": (settled_vol_ma5 / settled_vol_ma) if (settled_vol_ma and settled_vol_ma > 0) else float("nan"),
        "vp_harmony": vph,
        "vr_pct60": vr_pct60, "abspct_pct20": abspct_pct20, "spread_pct20": spread_pct20,
        "obv": float(last.get("obv", 0)) + (v if c > last_close else (-v if c < last_close else 0)),
        "range_vs_5d_avg": range_vs_5d_avg,  # v7 review C#2
        "is_polluting_bar": is_polluting,    # v7 review C#2
    }
    return pd.concat([df_settled, pd.DataFrame([new_row])], ignore_index=True)


def _load_ohlcv_weekly(
    code: str, weeks: int = 26, as_of: str | None = None
) -> Optional[pd.DataFrame]:
    """v9 §1: aggregate daily OHLCV into weekly bars.

    Anna Coulling: "Always look at the higher timeframe before judging the lower."
    Weekly view distinguishes ranging stocks (no real phase) from trending
    ones (clear markup/markdown) — daily-only systems get whipsawed in
    ranging stocks because daily noise looks like phase signals.

    Returns weekly OHLCV with columns: date (week-end), open, high, low, close, volume.
    Default 26 weeks needs ~180 daily bars; fall back gracefully if DB has less.
    """
    # Try to fetch as much daily history as possible. DB capped at ~200 days.
    # If as_of is far back, fewer bars available — try progressively smaller.
    df_daily = None
    for days_try in (min(weeks * 7 + 30, 200), 150, 100, 60):
        df_daily = _load_ohlcv(code, days=days_try, as_of=as_of)
        if df_daily is not None and len(df_daily) >= 30:
            break
    if df_daily is None or len(df_daily) < 30:
        return None
    df = df_daily.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    weekly = df.resample("W").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    weekly = weekly.reset_index()
    weekly["date"] = weekly["date"].dt.strftime("%Y-%m-%d")
    return weekly if len(weekly) >= 4 else None


def _load_ohlcv_monthly(
    code: str, months: int = 6, as_of: str | None = None
) -> Optional[pd.DataFrame]:
    """v9 §1: aggregate daily OHLCV into monthly bars.

    Monthly view shows the long-term cycle position — distinguishes early
    markup (1-2 month base, weak trend) from late markup (12+ month base,
    extended trend, BC risk). Default 6 months needs ~180 daily bars.
    """
    df_daily = None
    for days_try in (min(months * 31 + 15, 200), 150, 100, 60):
        df_daily = _load_ohlcv(code, days=days_try, as_of=as_of)
        if df_daily is not None and len(df_daily) >= 30:
            break
    if df_daily is None or len(df_daily) < 30:
        return None
    df = df_daily.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    monthly = df.resample("ME").agg({  # ME = Month-End (replaces deprecated 'M')
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    monthly = monthly.reset_index()
    monthly["date"] = monthly["date"].dt.strftime("%Y-%m-%d")
    return monthly if len(monthly) >= 3 else None


def _summarize_higher_timeframe(df: pd.DataFrame, periods: int = 12, label: str = "周") -> dict:
    """v9 §1: summarize higher-TF df for narrative section.

    Returns dict with: period_count, trend_pct, range_pct, position_in_range,
    direction_consistency, avg_volume_ratio. No phase classification — let
    the LLM read the numbers and decide.
    """
    if df is None or len(df) < 3:
        return {}
    recent = df.tail(periods)
    closes = recent["close"].values
    highs = recent["high"].values
    lows = recent["low"].values
    vols = recent["volume"].values

    period_count = len(recent)
    trend_pct = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] > 0 else 0
    period_high = float(highs.max())
    period_low = float(lows.min())
    # markup duration proxy: how many bars since the lowest close
    #   - high值 (近 N) → 持续 markup, multi-tf 高位警报权重应低 (Anna: don't fight trend)
    #   - 低值 (1-3) → 刚由低位反弹, 突破支持权重应高
    weeks_since_low = int(len(closes) - 1 - int(closes.argmin()))
    period_range_pct = (period_high - period_low) / closes[-1] * 100 if closes[-1] > 0 else 0
    position_in_range = (closes[-1] - period_low) / (period_high - period_low) if period_high > period_low else 0.5

    # 方向一致性：连续涨/跌的极端值
    diffs = pd.Series(closes).diff().dropna()
    up_count = int((diffs > 0).sum())
    down_count = int((diffs < 0).sum())
    direction_consistency = (up_count - down_count) / max(1, len(diffs))  # -1 to +1

    # 量比：最近 1 期 vs 前 N-1 期均值
    recent_vol = float(vols[-1])
    prior_avg_vol = float(vols[:-1].mean()) if len(vols) > 1 else recent_vol
    vol_ratio = recent_vol / prior_avg_vol if prior_avg_vol > 0 else 1.0

    return {
        "label": label,
        "period_count": period_count,
        "trend_pct": round(trend_pct, 1),
        "range_pct": round(period_range_pct, 1),
        "position_in_range": round(position_in_range, 2),
        "direction_consistency": round(direction_consistency, 2),
        "vol_ratio_recent": round(vol_ratio, 2),
        "high": round(period_high, 2),
        "low": round(period_low, 2),
        "current": round(float(closes[-1]), 2),
        "up_count": up_count,
        "down_count": down_count,
        "weeks_since_low": weeks_since_low,
    }


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


def _prev_trading_day(as_of: str, db_path: str | None = None, code: str | None = None) -> str | None:
    """Return the previous trading day (YYYY-MM-DD) before ``as_of`` based
    on actual dates in the daily_kline DB.

    If ``code`` is provided, the lookup is restricted to that stock's trading
    history (handles per-stock suspensions). Otherwise uses the global set of
    distinct dates across daily_kline.

    Returns None if no prior date exists in the DB. v7 review minor: also
    returns None on ``sqlite3.OperationalError`` (locked DB, missing file,
    schema drift) instead of bubbling up — callers treat the cold-start
    None and the transient-failure None identically.

    With no explicit ``db_path`` the configured market-history location is
    used, resolved at call time rather than baked from ``__file__`` — the
    hardcoded path ignored every data-dir override (deploys, tests).
    """
    if db_path is None:
        from alpha_agents.data import market_history
        db_path = str(market_history.DB_PATH)
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.OperationalError as exc:
        logger.debug("_prev_trading_day: sqlite3.connect failed (%s) — treating as cold start", exc)
        return None
    try:
        if code:
            row = conn.execute(
                "SELECT MAX(date) FROM daily_kline WHERE date < ? AND code = ?",
                (as_of[:10], code),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT MAX(date) FROM daily_kline WHERE date < ?",
                (as_of[:10],),
            ).fetchone()
        return row[0] if row and row[0] else None
    except sqlite3.OperationalError as exc:
        logger.debug("_prev_trading_day: query failed (%s) — treating as cold start", exc)
        return None
    finally:
        conn.close()


def _compute_derived(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Compute per-bar derived metrics: volume ratio, close position, spread, shadows."""
    df = df.copy()

    # Compute pct_change + bar_spread FIRST so the polluting mask can be
    # evaluated before vol_ma — limit-up bars' volumes must not pollute the
    # 20-day rolling mean (spec §3.1).
    hl_range = df["high"] - df["low"]
    df["bar_spread"] = hl_range / df["close"]
    df["pct_change"] = df["close"].pct_change()

    # v7 §3.1: exclude limit-up/down bars from BOTH vol_ma and the rolling-
    # percentile baseline. The polluting mask only needs pct_change +
    # bar_spread (+ optional code/name); compute it once here and reuse below.
    def _polluting_mask(row_df: pd.DataFrame) -> pd.Series:
        pct = row_df["pct_change"].abs()
        spread = row_df["bar_spread"]
        if "code" in row_df.columns:
            # Per-bar limit lookup. Most rows share the same code in practice
            # (single-stock df), so this is cheap.
            codes = row_df["code"].astype(str)
            names = row_df["name"].astype(str) if "name" in row_df.columns else pd.Series([""] * len(row_df), index=row_df.index)
            limits = pd.Series(
                [_daily_limit_pct(c, n) for c, n in zip(codes, names)],
                index=row_df.index,
            )
        else:
            limits = pd.Series([0.10] * len(row_df), index=row_df.index)
        near_limit = pct >= 0.95 * limits
        no_intraday = spread < 0.005
        one_letter = spread < 0.001
        return one_letter | (near_limit & no_intraday)

    polluting = _polluting_mask(df)

    # vol_ma computed from CLEAN volume — limit-up days' inflated volume is
    # excluded so subsequent normal bars' volume_ratio reads true. We use
    # min_periods=max(1, window // 2) so the rolling mean still produces
    # values when some bars in the window are NaN due to filtering.
    clean_volume = df["volume"].where(~polluting)
    df["vol_ma"] = clean_volume.rolling(window=window, min_periods=max(1, window // 2)).mean()
    df["volume_ratio"] = df["volume"] / df["vol_ma"]

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
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_trend_ratio"] = df["vol_ma5"] / df["vol_ma"]

    # Rolling percentile ranks — replace fixed thresholds with stock-adaptive
    # comparisons to recent norms (Anna Coulling: "qualitative comparison
    # relative to recent norms"). Each bar's value is ranked within its own
    # trailing window so a hot small-cap and a quiet bank-stock both get
    # their "high volume" / "wide bar" detected against the right baseline.
    # We reuse the polluting mask computed above.
    # .where keeps values where mask is True (NOT polluting), drops to NaN otherwise.
    clean_vol_ratio = df["volume_ratio"].where(~polluting)
    clean_pct_change = df["pct_change"].where(~polluting)
    clean_bar_spread = df["bar_spread"].where(~polluting)
    df["vr_pct60"] = clean_vol_ratio.rolling(window=60, min_periods=20).rank(pct=True)
    df["abspct_pct20"] = clean_pct_change.abs().rolling(window=20, min_periods=10).rank(pct=True)
    df["spread_pct20"] = clean_bar_spread.rolling(window=20, min_periods=10).rank(pct=True)
    df["is_polluting_bar"] = polluting  # exposed for scanner candidate-pool logic in §3.1

    # v5.2: per-bar range relative to its prior-5-day average range. Used
    # by the validator's range_vs_5d_avg_x check — replaces v5.1's
    # hardcoded 1.5 floor with a per-stock 60d 90th-percentile cutoff.
    bar_range = df["high"] - df["low"]
    prior_5d_mean = bar_range.shift(1).rolling(window=5, min_periods=3).mean()
    df["range_vs_5d_avg"] = bar_range / prior_5d_mean

    # v10.3 (Gemini): ATR-relative range, robust against the 5d-mean blind
    # spot — after a tight Spring/吸筹尾声 consolidation the 5d basis
    # collapses, making any normal bar look "wide". 14-period ATR uses
    # true_range = max(H-L, |H-prev_C|, |L-prev_C|) so prior closes anchor
    # the volatility scale, surviving that collapse.
    prev_close = df["close"].shift(1)
    true_range = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr_14"] = true_range.rolling(window=14, min_periods=7).mean()
    df["range_vs_atr"] = bar_range / df["atr_14"]

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


def _compute_5d_vph(df: pd.DataFrame) -> tuple[str, str, float, float]:
    """5-day volume-price harmony (Anna 第一层 challenge gate input).

    Returns (label, direction, up_vol_sum, down_vol_sum) where:
      - label: human-readable string for prompt display
      - direction: "bullish" / "bearish" / "neutral" — used by validator
      - up_vol_sum / down_vol_sum: cumulative settled volume

    v5.1 #2: single source of truth — both ``_format_text`` (LLM-input
    side) and ``_phase_guard_context_from_df`` (validator side) call
    this helper. Filters today's intraday bar from the rolling window
    (settled-only) so the 14:00 vs 14:55 reading does not flip.
    """
    today_str = _dt.now().strftime("%Y-%m-%d")
    settled = df[df["date"].astype(str) != today_str]
    if len(settled) < 5:
        settled = df  # too short — fall back to whatever we have
    last5 = settled.tail(5)
    up_vol = float(last5.loc[last5["close"] > last5["open"], "volume"].sum())
    down_vol = float(last5.loc[last5["close"] < last5["open"], "volume"].sum())
    if down_vol <= 0 and up_vol > 0:
        return "bullish (跌日无量)", "bullish", up_vol, down_vol
    if up_vol <= 0 and down_vol > 0:
        return "bearish (涨日无量)", "bearish", up_vol, down_vol
    if up_vol > 1.2 * down_vol:
        return (f"bullish (涨日累计量 {up_vol/max(down_vol,1):.2f}× 跌日累计量)",
                "bullish", up_vol, down_vol)
    if down_vol > 1.2 * up_vol:
        return (f"bearish (跌日累计量 {down_vol/max(up_vol,1):.2f}× 涨日累计量)",
                "bearish", up_vol, down_vol)
    return "neutral (双向相当)", "neutral", up_vol, down_vol


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
