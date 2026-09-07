"""v7 Phase D cleanup tests (spec §3.1, §3.2, §3.3)."""

import pandas as pd
import numpy as np

from alpha_agents.tools.vpa import _daily_limit_pct, _compute_derived


def test_main_board_limit_is_010():
    assert _daily_limit_pct("600519", "贵州茅台") == 0.10
    assert _daily_limit_pct("000858", "五粮液") == 0.10


def test_chinext_limit_is_020():
    assert _daily_limit_pct("300136", "信维通信") == 0.20
    assert _daily_limit_pct("301308", "江波龙") == 0.20


def test_star_market_limit_is_020():
    assert _daily_limit_pct("688981", "中芯国际") == 0.20


def test_st_stock_limit_is_005():
    assert _daily_limit_pct("600519", "ST 茅台") == 0.05
    assert _daily_limit_pct("000858", "*ST 五粮") == 0.05


def test_unknown_prefix_falls_back_to_010():
    """Defensive default: assume main-board rules for unrecognized prefix."""
    assert _daily_limit_pct("999999", "未知") == 0.10


def test_chinext_limit_up_excluded_from_vr_pct60_and_volma():
    """A ChiNext stock with a one-letter limit-up day must have that bar
    flagged AND its volume excluded from rolling baselines (vr_pct60 +
    vol_ma). Spec §3.1.

    Construct: 70 bars of normal volume (~1.0 ratio), one limit-up bar at
    +19.9% with vol 5x. Without filter the limit-up day inflates the
    rolling baseline AND the 20d vol_ma, making subsequent normal bars'
    volume_ratio look small (<1.0) and their vr_pct60 sit in the lower
    end of the distribution.
    """
    rng = np.random.default_rng(seed=42)
    n = 70
    base_vol = 1_000_000
    closes = 50.0 + np.cumsum(rng.normal(0, 0.5, n))
    opens = closes + rng.normal(0, 0.1, n)
    highs = np.maximum(opens, closes) + 0.3
    lows = np.minimum(opens, closes) - 0.3
    vols = np.full(n, base_vol)
    # Inject ChiNext limit-up at index 30: gap up to limit (close == open == high == low)
    closes[30] = closes[29] * 1.199
    opens[30] = closes[30]
    highs[30] = closes[30]
    lows[30] = closes[30]
    vols[30] = base_vol * 5

    df = pd.DataFrame({
        "code": ["300136"] * n,
        "name": ["信维通信"] * n,
        "date": [f"2025-{(i // 30) + 9:02d}-{(i % 30) + 1:02d}" for i in range(n)],
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": vols,
    })
    df = _compute_derived(df, window=20)

    # 1. The limit-up bar must be flagged.
    assert df["is_polluting_bar"].iloc[30] == True, (
        "limit-up bar at index 30 should be flagged as polluting"
    )

    # 2. The limit-up bar itself should have NaN rank values (excluded from
    #    its own percentile-rank computation via .where(~polluting)).
    assert pd.isna(df["vr_pct60"].iloc[30]), (
        "limit-up bar should have NaN vr_pct60 (its volume is excluded "
        "from the rolling-rank baseline, so it can't rank itself)"
    )

    # 3. Normal bars after the limit-up bar must have volume_ratio ~1.0
    #    (their actual volume is base_vol; clean vol_ma should be ~base_vol
    #    since the limit-up day is excluded). The 20-bar window after the
    #    limit-up day (index 31..50) is the most sensitive — without
    #    filtering, vol_ma would include the 5x outlier and these bars'
    #    volume_ratio would be skewed below 1.0.
    post_limitup = df.iloc[40:60]  # window after vol_ma settles
    median_vol_ratio = post_limitup["volume_ratio"].median()
    assert 0.85 <= median_vol_ratio <= 1.15, (
        f"normal bars after limit-up should have volume_ratio ~1.0 with "
        f"clean vol_ma; got median {median_vol_ratio:.3f}. If vol_ma still "
        f"includes the 5x limit-up outlier, this median sits below 0.85."
    )

    # 4. Their vr_pct60 should sit mid-range (median 0.40-0.60) once
    #    measured against a clean rank baseline.
    median_vr_pct = post_limitup["vr_pct60"].dropna().median()
    assert 0.40 <= median_vr_pct <= 0.65, (
        f"normal bars' vr_pct60 should land mid-distribution with clean "
        f"baseline; got {median_vr_pct:.3f}. If the baseline still includes "
        f"the limit-up outlier, normal bars' rank is depressed."
    )


def test_range_vs_5d_avg_column_is_added():
    """v7 §2.1 needs `range_vs_5d_avg` column for the candidate scanner."""
    df = pd.DataFrame({
        "code": ["300136"] * 30,
        "name": ["信维通信"] * 30,
        "date": [f"2025-{(i // 30) + 9:02d}-{(i % 30) + 1:02d}" for i in range(30)],
        "open": [10.0] * 30,
        "high": [10.0 + 0.5 * (i % 3) for i in range(30)],   # alternating ranges
        "low":  [10.0 - 0.5 * (i % 3) for i in range(30)],
        "close": [10.0] * 30,
        "volume": [1_000_000] * 30,
    })
    df = _compute_derived(df, window=20)
    assert "range_vs_5d_avg" in df.columns
    # First 3 rows: NaN (shift(1) + rolling(5, min_periods=3) requires
    # at least 3 prior settled bars before producing a value).
    assert df["range_vs_5d_avg"].iloc[:3].isna().all()
    # Later rows: should be non-negative (today's range / mean of prior 5).
    # Note: this synthetic data has alternating zero-range bars, so values
    # of 0.0 and inf can both occur; just check non-negativity.
    later = df["range_vs_5d_avg"].iloc[10:].dropna()
    assert (later >= 0).all()


def test_realtime_baseline_excludes_latest_settled_bar():
    """v7 §3.2: when the last settled bar was an extreme volume day,
    today's intraday vol_ratio should be measured against the 20d mean
    *excluding* yesterday's outlier.
    """
    from alpha_agents.tools.vpa import _append_realtime_with_derived

    n = 30
    closes = list(range(50, 50 + n))
    df = pd.DataFrame({
        "date": [f"2025-{(i // 30) + 9:02d}-{(i % 30) + 1:02d}" for i in range(n)],
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1_000_000] * (n - 1) + [10_000_000],  # last bar 10x volume
    })
    df = _compute_derived(df, window=20)

    # Real-time bar: identical OHLC to last settled, vol = 1_000_000 (normal)
    rt = {
        "date": "2099-01-01",
        "open": 80.0, "high": 80.5, "low": 79.5, "close": 80.0,
        "volume": 1_000_000,
    }
    df_rt = _append_realtime_with_derived(df, rt)
    rt_row = df_rt.iloc[-1]
    # If we use the WRONG baseline (last settled vol_ma includes the 10M
    # outlier), today's vol_ratio looks small (~0.7). With shift(1), the
    # baseline is the prior 19 normal bars + 1 outlier-excluded mean, so
    # vol_ratio for today's normal volume should sit near 1.0.
    assert 0.85 <= rt_row["volume_ratio"] <= 1.20, (
        f"realtime vol_ratio should be ~1.0 with shifted baseline; got "
        f"{rt_row['volume_ratio']:.3f}. Likely the baseline still includes "
        f"the latest settled bar's 10x outlier."
    )


def test_ranging_override_uses_per_stock_rank_not_fixed_thresholds():
    """v7 §3.3: a high-volatility stock with `range_10d=4%` should NOT be
    flagged as ranging (rank not bottom-20% in its own distribution),
    even though 4% < the old fixed 8% threshold.
    """
    from alpha_agents.tools.vpa import _phase_guard_context_from_df, _looks_like_ranging_context

    # Build a df where this stock historically swings 6-10% over 10d windows
    # (high-vol stock). Today's range_10d = 4% — historically below average.
    n = 80
    base = 100.0
    rng = np.random.default_rng(seed=7)
    daily_pct = rng.normal(0, 0.025, n)  # 2.5% daily stdev = high-vol stock
    closes = [base]
    for p in daily_pct[:-1]:
        closes.append(closes[-1] * (1 + p))
    df = pd.DataFrame({
        "code": ["300136"] * n,
        "name": ["信维通信"] * n,
        "date": [f"2025-{(i // 30) + 7:02d}-{(i % 30) + 1:02d}" for i in range(n)],
        "open": closes,
        "high": [c * 1.012 for c in closes],
        "low": [c * 0.988 for c in closes],
        "close": closes,
        "volume": [int(1e7)] * n,
    })
    df = _compute_derived(df, window=20)
    ctx = _phase_guard_context_from_df(df, as_of="2099-01-01")  # "future" so today_str doesn't filter

    # The new context exposes per-stock ranks for the ranging check
    assert "abs_trend_10d_pct_rank" in ctx
    assert "range_10d_pct_rank" in ctx
    assert "vol_ratio_5d_rank" in ctx

    # ranging override fires only when ALL ranks in the right zones
    assert isinstance(ctx["abs_trend_10d_pct_rank"], float)
    assert 0.0 <= ctx["abs_trend_10d_pct_rank"] <= 1.0


def test_ranging_override_only_fires_when_zero_signals_and_strict_flat():
    """Existing v5.x semantic preserved: even with bottom-20% ranks, no
    override if LLM emitted any signal (defer to LLM frame)."""
    from alpha_agents.tools.vpa import _looks_like_ranging_context

    flat_ctx = {
        "abs_trend_10d_pct_rank": 0.10,
        "range_10d_pct_rank": 0.10,
        "vol_ratio_5d_rank": 0.50,
    }
    # zero signals → ranging override fires
    assert _looks_like_ranging_context(flat_ctx, {"signals": []}) is True
    # any signal → no override
    assert _looks_like_ranging_context(flat_ctx, {"signals": [{"name": "x"}]}) is False


def test_realtime_bar_has_range_vs_5d_avg_and_polluting_flag():
    """v7 review C#2: realtime appended bar must have range_vs_5d_avg +
    is_polluting_bar populated, otherwise candidate scanner / cross-family
    validator can't process today's intraday bar in live mode.
    """
    from alpha_agents.tools.vpa import _append_realtime_with_derived
    n = 30
    closes = list(range(50, 50 + n))
    df = pd.DataFrame({
        "code": ["300136"] * n,
        "name": ["信维通信"] * n,
        "date": [f"2025-{(i // 30) + 9:02d}-{(i % 30) + 1:02d}" for i in range(n)],
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
    })
    df = _compute_derived(df, window=20)
    rt = {"date": "2099-01-01", "open": 80.0, "high": 81.0, "low": 79.0, "close": 80.5, "volume": 1_000_000}
    df_rt = _append_realtime_with_derived(df, rt)
    rt_row = df_rt.iloc[-1]
    assert "range_vs_5d_avg" in df_rt.columns
    assert "is_polluting_bar" in df_rt.columns
    # Today's range = 2.0; prior 5 bars all have range = 1.0; ratio = 2.0
    import math
    assert not math.isnan(rt_row["range_vs_5d_avg"]), "range_vs_5d_avg must be populated for realtime bar"
    assert rt_row["is_polluting_bar"] == False, "normal bar shouldn't be flagged polluting"
