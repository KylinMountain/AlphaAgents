"""Candidate climax-bar scanner — v7 §2.1.

Pre-filter "interesting" bars from the recent settled window so the LLM
can pick a specific candidate id rather than fabricating one.
"""

import math

import pandas as pd


def _scan_climax_candidates(
    df: pd.DataFrame,
    scan_window: int = 20,
    filter_vol_pctile: float = 0.75,
    filter_abspct_pctile: float = 0.75,
    filter_spread_pctile: float = 0.75,
    max_candidates: int = 5,
) -> list[dict]:
    """v7 §2.1: pre-filter "interesting" bars from the last ``scan_window``
    settled bars; package each as a dict with full numerical features.

    Recency principle: defaults of 20-day window + top-5 cap focus the LLM
    on the past ~month. Climaxes older than that are mostly absorbed into
    prior_state memory (yesterday's phase already reflects them), so they
    don't drive today's judgment. Callers can override with larger windows
    for backtesting analysis.

    The OR-of-three filter (vol / abspct / spread percentile) captures both
    climactic (high vol + wide range) and breakout (high vol + narrow body)
    events. No labels — Anna's BC/SC/UTAD/etc. classification is the LLM's
    job. Code only flags "physically extreme by the stock's own recent norm".

    Filter thresholds are tunable parameters (NOT picked numbers per spec
    §0). They are pinned by the scanner_recall test (§5.1).
    """
    if df is None or len(df) < scan_window:
        return []
    if "vr_pct60" not in df.columns:
        return []  # _compute_derived hasn't run

    recent = df.tail(scan_window)
    vol_pct = recent["vr_pct60"].fillna(0)
    abspct_pct = recent["abspct_pct20"].fillna(0)
    spread_pct = recent["spread_pct20"].fillna(0)
    # Polluting bars (limit-up/down, one-letter) have NaN ranks because their
    # volume is excluded from the rolling-rank baseline. Use raw volume_ratio
    # as a fallback so a high-vol limit-up day can still be a candidate. The
    # 2.0 threshold matches "extreme volume regardless of rank".
    is_polluting = recent.get("is_polluting_bar", pd.Series(False, index=recent.index))
    fallback_vol = is_polluting & (recent["volume_ratio"].fillna(0) >= 2.0)
    triggers = (
        (vol_pct >= filter_vol_pctile)
        | (abspct_pct >= filter_abspct_pctile)
        | (spread_pct >= filter_spread_pctile)
        | fallback_vol
    )
    triggered = recent[triggers].copy()
    if triggered.empty:
        return []

    # Rank by max-of-three-percentiles for the cap. Polluting bars have NaN
    # ranks (excluded from baseline), so when ranked-sorted they'd drop to
    # the back and be cut by max_candidates. For polluting bars cleared by
    # the raw-vol fallback, substitute 1.0 — a 2x+ volume on a limit-up day
    # IS a top-tier signal, just baseline-incomparable.
    triggered["_rank_score"] = triggered[["vr_pct60", "abspct_pct20", "spread_pct20"]].max(axis=1, skipna=True)
    fallback_mask = triggered.get("is_polluting_bar", pd.Series(False, index=triggered.index)) & (
        triggered["volume_ratio"].fillna(0) >= 2.0
    )
    triggered.loc[fallback_mask & triggered["_rank_score"].isna(), "_rank_score"] = 1.0
    triggered = triggered.sort_values("_rank_score", ascending=False).head(max_candidates)
    triggered = triggered.sort_values("date")  # back to chronological order for stable output

    candidates = []
    for _, row in triggered.iterrows():
        candidates.append(_build_candidate_dict(row, df))
    return candidates


def _build_candidate_dict(row: pd.Series, df_full: pd.DataFrame) -> dict:
    """Pure helper: package one bar's features into the candidate schema.
    Forward-looking ``post_bar_reverse_3d`` populated by Task 2.2.
    """
    def _safe_float(value, default: float = 0.0) -> float:
        # `float(nan or 0)` returns nan because NaN is truthy in Python; use
        # explicit isnan check so polluting bars (with NaN ranks) serialize as
        # the default rather than poisoning downstream comparators.
        try:
            f = float(value)
            return default if math.isnan(f) else f
        except (TypeError, ValueError):
            return default

    date_str = str(row.get("date", ""))
    return {
        "id": f"cand-{date_str}",
        "date": date_str,
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": _safe_float(row.get("volume", 0)),
        "bar_type": str(row.get("bar_type", "")),
        "vol_ratio": _safe_float(row.get("volume_ratio", 0)),
        "vol_ratio_pct": _safe_float(row.get("vr_pct60", 0)),
        "range_vs_5d_avg": _safe_float(row.get("range_vs_5d_avg", 0)),
        # v10.3 (Gemini): ATR-relative range — robust against narrow-range
        # baseline collapse that range_vs_5d_avg suffers from.
        "range_vs_atr": _safe_float(row.get("range_vs_atr", 0)),
        "atr_14": _safe_float(row.get("atr_14", 0)),
        "bar_spread": _safe_float(row.get("bar_spread", 0)),
        "spread_pct": _safe_float(row.get("spread_pct20", 0)),
        "close_position": _safe_float(row.get("close_position", 0.5), default=0.5),
        "upper_shadow": _safe_float(row.get("upper_shadow", 0)),
        "lower_shadow": _safe_float(row.get("lower_shadow", 0)),
        "pct_change": _safe_float(row.get("pct_change", 0)),
        "abspct_pct": _safe_float(row.get("abspct_pct20", 0)),
        "obv": _safe_float(row.get("obv", 0)),
        **_compute_post_bar_reverse(row, df_full),
    }


def _compute_post_bar_reverse(row: pd.Series, df_full: pd.DataFrame) -> dict:
    """Forward-looking confirmation for a candidate bar.

    Returns max signed cumulative return over the next 1-3 bars after
    the candidate. The sign is based on the most extreme cumulative move
    in either direction. Used by validator's cross-family rule.
    """
    cand_date = str(row.get("date", ""))
    candidate_idx = df_full.index[df_full["date"].astype(str) == cand_date]
    if len(candidate_idx) == 0:
        return {"post_bar_reverse_3d": None, "post_bars_observed": 0}
    idx = candidate_idx[0]
    cand_close = float(row["close"])
    if cand_close <= 0:
        return {"post_bar_reverse_3d": None, "post_bars_observed": 0}
    next_3 = df_full.iloc[df_full.index.get_loc(idx) + 1:][:3]
    n_obs = len(next_3)
    if n_obs == 0:
        return {"post_bar_reverse_3d": None, "post_bars_observed": 0}
    # Compute cumulative return from candidate's close to each next-bar's close
    rets = (next_3["close"].astype(float) - cand_close) / cand_close
    # Pick the most-extreme cumulative move (preserves sign)
    max_pos = rets.max() if (rets > 0).any() else 0.0
    max_neg = rets.min() if (rets < 0).any() else 0.0
    extreme = max_pos if abs(max_pos) >= abs(max_neg) else max_neg
    return {"post_bar_reverse_3d": float(extreme), "post_bars_observed": int(n_obs)}
