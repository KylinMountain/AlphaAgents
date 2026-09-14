"""The bars themselves, and the columns derived from them.

OHLCV loading (daily / weekly / monthly / 60min), the realtime bar splice, and
the derived metrics computed per bar — volume-price harmony, relative strength,
OBV trend, volume regime. Moved out of ``data.py`` as one subject: everything
here answers "what do the bars say", while what stayed there answers "what setup
is this", and the two are edited for different reasons.

``data.py`` re-exports these names, so ``from alpha_agents.tools.vpa.data import
_load_ohlcv`` — which the adapters, the scripts and eight test files all do —
keeps working unchanged.
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
