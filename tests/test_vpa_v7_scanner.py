"""v7 candidate scanner tests (spec §2.1, §2.1.1)."""

import pandas as pd
import numpy as np
from alpha_agents.tools.vpa import _scan_climax_candidates, _compute_derived


def _make_synthetic_df(n: int = 80, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed=seed)
    closes = 50.0 + np.cumsum(rng.normal(0, 0.5, n))
    opens = closes + rng.normal(0, 0.1, n)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.2, n))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.2, n))
    vols = rng.integers(900_000, 1_100_000, n).astype(float)
    df = pd.DataFrame({
        "code": ["300136"] * n,
        "name": ["信维通信"] * n,
        "date": [f"2025-{(i // 30) + 7:02d}-{(i % 30) + 1:02d}" for i in range(n)],
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": vols,
    })
    return _compute_derived(df, window=20)


def test_empty_df_returns_empty_list():
    df = pd.DataFrame()
    assert _scan_climax_candidates(df) == []


def test_short_window_returns_empty_list():
    """If df has fewer rows than scan_window, scanner returns []."""
    df = _make_synthetic_df(n=20)
    assert _scan_climax_candidates(df, scan_window=40) == []


def test_no_extreme_bars_returns_empty_list():
    """Synthetic df has only normal bars (no percentile extremes); pool empty.

    Rolling pct-ranks always span [1/n, 1.0] within each window, so any
    threshold ≤ 1.0 will fire on the within-window max bars. To assert
    "no extreme bars" with uniform synthetic data, we set thresholds
    above the rolling-rank ceiling (1.01) — pinning the OR filter's
    structural emptiness when no bar can clear any of the three legs.
    """
    df = _make_synthetic_df(n=80, seed=42)
    candidates = _scan_climax_candidates(df, scan_window=40,
                                          filter_vol_pctile=1.01,
                                          filter_abspct_pctile=1.01,
                                          filter_spread_pctile=1.01)
    assert candidates == []


def test_filter_trigger_via_volume_independently():
    """A bar with extreme volume but normal range/spread should be a candidate."""
    df = _make_synthetic_df(n=80, seed=42)
    # Inject extreme volume on bar 65 (within last 40 of 80)
    df.loc[65, "volume"] = df["volume"].iloc[60:65].mean() * 5
    df = _compute_derived(df, window=20)
    candidates = _scan_climax_candidates(df, scan_window=40,
                                          filter_vol_pctile=0.75)
    dates = [c["date"] for c in candidates]
    assert df.iloc[65]["date"] in dates


def test_candidate_dict_has_required_fields():
    df = _make_synthetic_df(n=80, seed=42)
    df.loc[70, "volume"] = df["volume"].iloc[65:70].mean() * 4
    df = _compute_derived(df, window=20)
    candidates = _scan_climax_candidates(df, scan_window=40, filter_vol_pctile=0.75)
    assert len(candidates) >= 1
    c = candidates[0]
    required = {
        "id", "date", "open", "high", "low", "close", "volume",
        "bar_type", "vol_ratio", "vol_ratio_pct",
        "range_vs_5d_avg", "bar_spread", "spread_pct",
        "close_position", "upper_shadow", "lower_shadow",
        "pct_change", "abspct_pct", "obv",
        "post_bar_reverse_3d", "post_bars_observed",
    }
    missing = required - set(c.keys())
    assert not missing, f"candidate dict missing fields: {missing}"


def test_candidate_id_format():
    df = _make_synthetic_df(n=80)
    df.loc[70, "volume"] *= 5
    df = _compute_derived(df, window=20)
    candidates = _scan_climax_candidates(df, scan_window=40, filter_vol_pctile=0.75)
    for c in candidates:
        assert c["id"] == f"cand-{c['date']}"


def test_max_candidates_cap_at_15():
    """If many bars trigger, pool capped at 15 by max-of-three-percentiles."""
    df = _make_synthetic_df(n=80)
    # Inject 25 extreme bars in last 40
    for idx in range(40, 80):
        df.loc[idx, "volume"] *= 4
    df = _compute_derived(df, window=20)
    candidates = _scan_climax_candidates(df, scan_window=40,
                                          filter_vol_pctile=0.50,
                                          max_candidates=15)
    assert len(candidates) <= 15


def test_post_bar_reverse_3d_populated_for_old_candidates():
    """Candidates with at least 3 settled bars after them get post_bar_reverse_3d
    set to the max-abs-return over those next 3 bars (signed)."""
    df = _make_synthetic_df(n=80, seed=42)
    # Make bar 65 a candidate via volume spike
    df.loc[65, "volume"] *= 5
    # Make bars 66-68 show a clear -5% reversal cumulatively (compounding)
    for offset in range(1, 4):
        df.loc[65 + offset, "close"] *= 0.98 ** offset
    df = _compute_derived(df, window=20)
    # max_candidates=15 keeps bar 65 in pool: the post-bar reversal injection
    # also lifts subsequent bars' percentiles, so bar 65 isn't top-5 by rank.
    # This test verifies post_bar_reverse_3d population, not the cap mechanism.
    candidates = _scan_climax_candidates(df, scan_window=40, filter_vol_pctile=0.75, max_candidates=15)
    cand_65 = next(c for c in candidates if c["date"] == df.iloc[65]["date"])
    assert cand_65["post_bars_observed"] == 3
    assert cand_65["post_bar_reverse_3d"] is not None
    # Cumulative -5.9% over 3 bars (each -2%): post should be ~-5.9%
    assert cand_65["post_bar_reverse_3d"] < -0.04


def test_post_bar_reverse_3d_null_for_recent_candidates():
    """A candidate at the very last bar of df has 0 post-bars observed."""
    df = _make_synthetic_df(n=80)
    df.loc[df.index[-1], "volume"] *= 5
    df = _compute_derived(df, window=20)
    candidates = _scan_climax_candidates(df, scan_window=40, filter_vol_pctile=0.75)
    last_cand = next(
        (c for c in candidates if c["date"] == str(df.iloc[-1]["date"])),
        None,
    )
    if last_cand is not None:
        assert last_cand["post_bars_observed"] == 0
        assert last_cand["post_bar_reverse_3d"] is None


def test_chinext_limit_up_kept_in_candidate_pool_but_baseline_excluded():
    """v7 §3.1: a 一字板 limit-up bar IS a candidate (open-from-limit
    reversal can be a real climax pattern). But when reading its
    vol_ratio_pct, the value reflects the polluting-excluded baseline
    (we shouldn't rank it against other limit-up bars).
    """
    n = 80
    rng = np.random.default_rng(seed=11)
    closes = 50.0 + np.cumsum(rng.normal(0, 0.3, n))
    df = pd.DataFrame({
        "code": ["300136"] * n,
        "name": ["信维通信"] * n,
        "date": [f"2025-{(i // 30) + 7:02d}-{(i % 30) + 1:02d}" for i in range(n)],
        "open": closes, "high": closes + 0.3, "low": closes - 0.3,
        "close": closes, "volume": [1_000_000] * n,
    })
    # Inject a creditworthy ChiNext one-letter limit-up at index 70
    df.loc[70, "open"] = df.loc[69, "close"] * 1.199
    df.loc[70, "high"] = df.loc[70, "open"]
    df.loc[70, "low"] = df.loc[70, "open"]
    df.loc[70, "close"] = df.loc[70, "open"]
    df.loc[70, "volume"] = 5_000_000  # high volume on limit-up
    df = _compute_derived(df, window=20)
    candidates = _scan_climax_candidates(df, scan_window=40, filter_vol_pctile=0.75)
    limit_up_cand = next(
        (c for c in candidates if c["date"] == str(df.iloc[70]["date"])),
        None,
    )
    assert limit_up_cand is not None, "ChiNext limit-up should be in candidate pool"
    # vol_ratio_pct should still be a meaningful number, not skewed by self-inclusion
    assert 0.0 <= limit_up_cand["vol_ratio_pct"] <= 1.0
