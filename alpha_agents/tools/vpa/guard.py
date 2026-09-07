"""Phase state machine + ranging override + vp_harmony correction.

Layered guard applied after LLM parsing:
  1. Phase hygiene — same-family subphase smoothing and ranging override
  2. Cross-family validator — see ``validator.py``
  3. vp_harmony audit — records alignment without mutating confirmation_level
"""

import logging
from datetime import datetime as _dt

import pandas as pd

from .data import _compute_5d_vph
from .validator import (
    _validate_absorbed_test_signals,
    _validate_vph_conflict_evidence,
)
from .verdict import (
    BULLISH_PHASE_FAMILIES,
    BEARISH_PHASE_FAMILIES,
    PHASE_STAGE_RANK_KEYWORDS,
    PHASE_TRANSITION_REQUIREMENTS,
    _coerce_confidence,
    _contains_phase_keyword,
    _derive_confirmation_level,
    _extract_previous_state,
    _is_confirmed_decisive_signal,
    _is_structural_phase_change,
    _normalize_phase,
    _normalize_phase_change,
    _phase_family,
    _signal_requires_action_confirmation,
    _signal_text,
)

logger = logging.getLogger(__name__)


_BULLISH_DIRS = ("看多", "偏多")
_BEARISH_DIRS = ("看空", "偏空")


def _has_decisive_directional_signal(data: dict, target_family: str) -> bool:
    """Looser structural confirmation for post-guard phase reads.

    This intentionally does not use LLM confidence because older hard-guarded
    caches clipped confidence to 0.55 even when the raw report contained SOS/SOW.
    """
    direction = data.get("direction", "")
    signals = [s for s in data.get("signals", []) or [] if isinstance(s, dict)]
    confirmed_signals = [s for s in signals if _is_confirmed_decisive_signal(s)]
    if not confirmed_signals:
        return False

    if target_family in BULLISH_PHASE_FAMILIES and direction not in {"看多", "偏多"}:
        return False
    if target_family in BEARISH_PHASE_FAMILIES and direction not in {"看空", "偏空"}:
        return False

    joined = " ".join(_signal_text(s).lower() for s in confirmed_signals)
    if target_family == "markup":
        return _contains_phase_keyword(
            joined,
            ("sos", "sign of strength", "强势确认", "放量突破", "突破"),
        )
    if target_family == "distribution":
        return _contains_phase_keyword(
            joined,
            ("派发", "distribution", "bc", "buying climax", "买入高潮顶部",
             "psy", "utad", "初步供应", "放量上影"),
        )
    if target_family == "markdown":
        return _contains_phase_keyword(
            joined,
            ("sow", "sign of weakness", "弱势确认", "跌破", "破位"),
        )
    if target_family == "accumulation":
        return _contains_phase_keyword(
            joined,
            ("spring", "lps", "sc", "selling climax", "恐慌抛售高潮",
             "最后支撑", "放量止跌", "底部吸收"),
        )
    return False


def _requires_phase_transition_confirmation(prev_family: str, new_family: str) -> bool:
    if not prev_family or not new_family or prev_family == new_family:
        return False
    if "neutral" in {prev_family, new_family}:
        return False
    if "unknown" in {prev_family, new_family}:
        return False
    return True


def _is_allowed_phase_transition(prev_family: str, new_family: str) -> bool:
    if not prev_family or not new_family or prev_family == new_family:
        return True
    if "neutral" in {prev_family, new_family}:
        return True
    if "unknown" in {prev_family, new_family}:
        return True
    return (prev_family, new_family) in PHASE_TRANSITION_REQUIREMENTS


def _phase_stage_rank(phase: object) -> int:
    p = _normalize_phase(phase)
    if not p:
        return 0
    for keyword, rank in PHASE_STAGE_RANK_KEYWORDS:
        if keyword in p:
            return rank
    return 1


def _has_same_family_upgrade_evidence(data: dict) -> bool:
    signals = [s for s in data.get("signals", []) or [] if isinstance(s, dict)]
    return any(_is_confirmed_decisive_signal(s) for s in signals)


def _looks_like_ranging_context(phase_context: dict | None, data: dict) -> bool:
    """v7 §3.3: per-stock rank-based ranging detection.

    Override LLM phase to 震荡 only when (a) LLM emitted no signals AND
    (b) all three per-stock ranks indicate "this is unusually flat for
    this particular stock":
      - abs_trend_10d_pct_rank ≤ 0.20 (trend magnitude in bottom 20% of recent history)
      - range_10d_pct_rank ≤ 0.20 (10d range in bottom 20%)
      - vol_ratio_5d_rank ∈ [0.30, 0.70] (volume in middle 40%, neither dry nor surge)

    The override remains conditional on zero signals (preserving v5.1 #4
    decision: trust the LLM's frame whenever it provides any evidence).
    """
    if not phase_context:
        return False
    signals = [s for s in data.get("signals", []) or [] if isinstance(s, dict)]
    if signals:
        return False
    abs_trend_rank = float(phase_context.get("abs_trend_10d_pct_rank", 0.5) or 0.5)
    range_rank = float(phase_context.get("range_10d_pct_rank", 0.5) or 0.5)
    vol_rank = float(phase_context.get("vol_ratio_5d_rank", 0.5) or 0.5)
    return (
        abs_trend_rank <= 0.20
        and range_rank <= 0.20
        and 0.30 <= vol_rank <= 0.70
    )


def _phase_guard_context_from_df(df: pd.DataFrame | None, as_of: str | None = None) -> dict:
    """Phase context for the validator.

    ``as_of`` (YYYY-MM-DD or YYYY-MM-DD HH:MM) is the analysis date — for
    backtest replays this is the historical day under analysis; for live
    runs it should be None (use wall-clock today). Used by:
      - settled_for_stats slice (drop today's intraday bar)
      - validator's null-post_bar cross-check (v5.1 #5)
    """
    if df is None or len(df) < 10:
        logger.debug("_phase_guard_context_from_df: insufficient data (len=%d), returning empty context", 0 if df is None else len(df))
        return {}
    recent = df.tail(10)
    last_close = float(recent["close"].iloc[-1])
    first_close = float(recent["close"].iloc[0])
    high = float(recent["high"].max())
    low = float(recent["low"].min())
    trend_10d = (last_close - first_close) / first_close * 100 if first_close > 0 else 0.0
    range_10d = (high - low) / last_close * 100 if last_close > 0 else 0.0
    # v7 review C#1: cross-family validator reads `extended_trend_20d_pct`
    # (mapped from `trend_20d_pct`) — populate it here using the same idiom
    # `_compute_context` uses (close[-1] vs close[-20]).
    closes = df["close"].astype(float)
    if len(closes) >= 20:
        c_now = float(closes.iloc[-1])
        c_20 = float(closes.iloc[-20])
        trend_20d = (c_now - c_20) / c_20 * 100 if c_20 > 0 else 0.0
    else:
        # Fewer than 20 bars: degrade gracefully — use the 10d figure so the
        # validator field is at least a real number rather than None.
        trend_20d = trend_10d
    avg_volume_ratio_5d = float(df["volume_ratio"].tail(5).mean()) if "volume_ratio" in df else 1.0

    # v5.1 #2: shared helper with _format_text — settled-only vph filter
    # happens inside _compute_5d_vph.
    _vph_label, vph, up_vol, down_vol = _compute_5d_vph(df)

    # Per-stock percentile cutoffs (Anna #1 critique). Top 10% of recent
    # magnitude — "qualitatively elevated, not normal noise". Falls back
    # to conservative defaults when the rolling window has not yet built up.
    def _p90(series: pd.Series, default: float) -> float:
        s = series.dropna()
        if len(s) < 20:
            return float(default)
        return float(s.quantile(0.90))

    # Settled-for-stats: prefer the as_of date over wall-clock now() for
    # filtering today's intraday bar (v5.1 #5: use explicit as_of from
    # caller rather than relying on _dt.now()). When as_of is None this
    # is live mode and wall-clock now() is the right cut.
    cut_date = (as_of[:10] if as_of else _dt.now().strftime("%Y-%m-%d"))
    settled_for_stats = df[df["date"].astype(str) != cut_date].tail(60)

    # v7 §3.3: per-stock distribution ranks for the ranging override (replaces
    # v5.x fixed thresholds 2.0% / 8.0% / [0.85, 1.05]). For each metric we
    # compute its rank against the trailing 60 settled bars' rolling values
    # of the SAME metric. The rolling values are computed by re-running the
    # 10d-trend / 10d-range / 5d-vol-ratio formulas across the historical
    # window so we have a comparable distribution.
    def _rank_against_history(today_value: float, history_series: pd.Series) -> float:
        """Percentile rank of today_value within history_series (0..1)."""
        s = history_series.dropna()
        if len(s) < 20:
            return 0.5  # insufficient history → neutral default
        return float((s <= today_value).sum() / len(s))

    settled_for_ranks = settled_for_stats
    # Rolling 10d trend pct: closes.pct_change(10) on each bar; we want abs.
    abs_trend_10d_history = settled_for_ranks["close"].pct_change(10).abs() * 100
    # Rolling 10d range pct: (high.rolling(10).max - low.rolling(10).min) / close * 100
    range_10d_history = (
        (settled_for_ranks["high"].rolling(10).max() - settled_for_ranks["low"].rolling(10).min())
        / settled_for_ranks["close"]
        * 100
    )
    # Rolling 5d volume_ratio mean
    vol_ratio_5d_history = (
        settled_for_ranks["volume_ratio"].rolling(5).mean()
        if "volume_ratio" in settled_for_ranks
        else pd.Series(dtype=float)
    )

    # Today's values (already computed above).
    abs_trend_10d_today = abs(trend_10d)
    range_10d_today = range_10d
    vol_ratio_5d_today = avg_volume_ratio_5d

    return {
        "trend_10d_pct": round(trend_10d, 2),
        "trend_20d_pct": round(trend_20d, 2),  # v7 review C#1: validator's extended_trend_20d_pct
        "range_10d_pct": round(range_10d, 2),
        "avg_volume_ratio_5d": round(avg_volume_ratio_5d, 2),
        "vp_harmony_score": vph,
        "vp_harmony_up_vol": round(up_vol, 0),
        "vp_harmony_down_vol": round(down_vol, 0),
        # v5.1 #5: pass as_of date so validator can cross-check climax_date
        # for null post_bar_reverse (rejecting old-climax-with-null bypass).
        "as_of_date": cut_date,
        # Validator percentile cutoffs (90th percentile of trailing 60 settled bars).
        # v5.2: shadow + range_vs_5d added so validator stops using
        # hardcoded 0.4 / 1.5. spread_p90 was removed in v5.1 #6.
        "vol_ratio_p90": round(_p90(settled_for_stats.get("volume_ratio", pd.Series(dtype=float)), 1.5), 2),
        "abspct_p90": round(_p90(settled_for_stats.get("pct_change", pd.Series(dtype=float)).abs(), 0.025), 4),
        "upper_shadow_p90": round(_p90(settled_for_stats.get("upper_shadow", pd.Series(dtype=float)), 0.4), 3),
        "lower_shadow_p90": round(_p90(settled_for_stats.get("lower_shadow", pd.Series(dtype=float)), 0.4), 3),
        "range_vs_5d_avg_p90": round(_p90(settled_for_stats.get("range_vs_5d_avg", pd.Series(dtype=float)), 1.5), 2),
        # v7 §3.3: per-stock distribution ranks for the ranging override.
        "abs_trend_10d_pct_rank": round(_rank_against_history(abs_trend_10d_today, abs_trend_10d_history), 3),
        "range_10d_pct_rank": round(_rank_against_history(range_10d_today, range_10d_history), 3),
        "vol_ratio_5d_rank": round(_rank_against_history(vol_ratio_5d_today, vol_ratio_5d_history), 3),
    }


def _strip_stale_phase_guard_reason(reason: object) -> str:
    text = str(reason or "")
    stale_prefix = "阶段延续："
    if not text.startswith(stale_prefix):
        return text
    if "；" in text:
        return text.split("；", 1)[1]
    return ""


def _refresh_confirmation_fields(data: dict, *, structural_phase_change_confirmed: bool | None = None) -> None:
    signals = [s for s in data.get("signals", []) or [] if isinstance(s, dict)]
    confirmed_any_signal = any(bool(s.get("confirmed", False)) for s in signals)
    confirmed_all_signals = bool(signals) and all(bool(s.get("confirmed", False)) for s in signals)
    action_signals = [s for s in signals if _signal_requires_action_confirmation(s)]
    action_confirmed_signal_count = sum(
        1 for s in action_signals if bool(s.get("confirmed", False))
    )
    decisive_confirmed_signals = [
        s for s in action_signals if _is_confirmed_decisive_signal(s)
    ]
    if structural_phase_change_confirmed is None:
        structural_phase_change_confirmed = _is_structural_phase_change(
            _normalize_phase_change(data.get("phase_change"))
        )
    level, tier = _derive_confirmation_level(
        action_signal_count=len(action_signals),
        decisive_confirmed_signal_count=len(decisive_confirmed_signals),
        structural_phase_change_confirmed=structural_phase_change_confirmed,
    )
    data["confirmed_any_signal"] = confirmed_any_signal
    data["confirmed_all_signals"] = confirmed_all_signals
    data["action_signal_count"] = len(action_signals)
    data["action_confirmed_signal_count"] = action_confirmed_signal_count
    data["decisive_confirmed_signal_count"] = len(decisive_confirmed_signals)
    data["structural_phase_change_confirmed"] = structural_phase_change_confirmed
    data["partial_confirmed"] = level >= 2
    data["action_confirmed"] = level >= 3
    data["confirmation_level"] = level
    data["confirmation_tier"] = tier
    data["confirmed"] = level >= 3


def _apply_vp_harmony_correction(data: dict, phase_context: dict | None) -> None:
    """Record 5-day volume-price harmony without changing C2/C3.

    Anna's first layer: "量价配合是真相". When direction agrees with the
    5-day vph, that is useful evidence; when it conflicts, it is a challenge.
    It is not structural confirmation. Do not mutate ``confirmation_level``:
    strategy gates read C2/C3 as structure-derived levels only.

    Mutates ``data`` only by adding audit fields:
      - vp_harmony_score
      - vp_harmony_alignment
      - vp_harmony_level_suggestion
      - vp_harmony_level_adjustment
    """
    score = (phase_context or {}).get("vp_harmony_score", "neutral")
    data["vp_harmony_score"] = score
    data["vp_harmony_alignment"] = "neutral"
    data["vp_harmony_level_suggestion"] = data.get("confirmation_level", 0)
    data["vp_harmony_level_adjustment"] = 0
    if score not in ("bullish", "bearish"):
        return
    direction = data.get("direction", "") or ""
    bull = direction in _BULLISH_DIRS
    bear = direction in _BEARISH_DIRS
    if not (bull or bear):
        return
    try:
        lvl = int(data.get("confirmation_level", 0))
    except (TypeError, ValueError):
        return
    same = (bull and score == "bullish") or (bear and score == "bearish")
    conflict = (bull and score == "bearish") or (bear and score == "bullish")
    data["vp_harmony_alignment"] = "same" if same else ("conflict" if conflict else "neutral")
    new_lvl = lvl
    if same:
        if lvl == 1:
            new_lvl = 2
        elif lvl == 2:
            new_lvl = 3
    elif conflict and not data.get("vph_conflict_validated"):
        if lvl == 3:
            new_lvl = 2
        elif lvl == 2:
            new_lvl = 1
    data["vp_harmony_level_suggestion"] = new_lvl
    data["vp_harmony_level_adjustment"] = new_lvl - lvl


def _apply_phase_state_guard(
    verdict_data: dict,
    previous_analysis: str,
    phase_context: dict | None = None,
) -> dict:
    """Apply Anna-style phase hygiene + vp_harmony correction after LLM
    parsing.

    Layers (in order):
      1. Phase hygiene — same-family subphase smoothing and a tight
         ranging override (only fires when LLM emits zero signals AND
         data is strictly flat).
      2. vph_conflict checklist validation — when phase direction
         disagrees with 5d vph, structurally verify the LLM's BC/SC
         numbers; demote to "初期 confirmed=false" on missing/failing
         evidence (Anna's first-layer challenge gate, code-enforced).
      3. vp_harmony audit — record 5d volume-price harmony alignment without
         changing structure-derived ``confirmation_level``.
    """
    result = _apply_phase_state_guard_core(verdict_data, previous_analysis, phase_context)
    _validate_vph_conflict_evidence(result, phase_context)
    # Phase 3: deterministic absorbed_supply_test signal verification.
    # Demotes any confirmed=true absorbed_test claim that violates v10.2
    # form rules (close_position > 0.6 + up day) when the signal date is
    # in the candidate pool.
    _validate_absorbed_test_signals(result, phase_context)
    _apply_vp_harmony_correction(result, phase_context)
    return result


def _apply_phase_state_guard_core(
    verdict_data: dict,
    previous_analysis: str,
    phase_context: dict | None = None,
) -> dict:
    """Phase hygiene only (no vp_harmony). See ``_apply_phase_state_guard``."""
    data = dict(verdict_data or {})
    data["confidence"] = _coerce_confidence(data.get("confidence"), default=0.5)
    data["phase_change"] = _normalize_phase_change(data.get("phase_change"))
    data.setdefault("warning_phase", "")
    data["raw_phase"] = data.get("raw_phase") or data.get("phase", "")
    data.setdefault("phase_state_changed", False)
    data["phase_guard_reason"] = ""
    data.setdefault("confirmed", False)
    data["reason"] = _strip_stale_phase_guard_reason(data.get("reason", ""))

    previous_state = _extract_previous_state(previous_analysis)
    if not previous_state:
        return data

    previous_phase = previous_state.get("phase", "") or ""
    previous_family = previous_state.get("phase_family", "") or _phase_family(previous_phase)
    raw_phase = data.get("raw_phase") or data.get("phase", "") or ""
    raw_family = _phase_family(raw_phase)
    data["previous_phase"] = previous_phase
    data["raw_phase"] = raw_phase

    if _looks_like_ranging_context(phase_context, data):
        if raw_family in {"accumulation", "distribution"}:
            data["raw_phase"] = raw_phase
            data["phase"] = "震荡"
            data["warning_phase"] = data.get("warning_phase") or raw_phase
            data["phase_state_changed"] = previous_family != "neutral"
            data["phase_guard_reason"] = "窄幅低量整理且无决定性VPA事件，主阶段压为震荡"
            data["confirmed"] = False
            phase_change = dict(data.get("phase_change") or {})
            phase_change.update({
                "from": previous_phase,
                "to": "震荡",
                "confirmed": False,
                "invalidated_by": "",
                "denial_level": "ranging",
            })
            data["phase_change"] = phase_change
            _refresh_confirmation_fields(data, structural_phase_change_confirmed=False)
            return data

    if previous_family == raw_family and previous_phase and raw_phase != previous_phase:
        previous_warning = previous_state.get("warning_phase", "") or ""
        if raw_phase == previous_warning and _has_same_family_upgrade_evidence(data):
            data["phase"] = raw_phase
            data["raw_phase"] = raw_phase
            data["phase_state_changed"] = True
            data["phase_guard_reason"] = ""
            data["reason"] = _strip_stale_phase_guard_reason(data.get("reason", ""))
            _refresh_confirmation_fields(data)
            return data
        data["raw_phase"] = raw_phase
        data["phase"] = previous_phase
        data["warning_phase"] = data.get("warning_phase") or raw_phase
        data["phase_state_changed"] = False
        data["phase_guard_reason"] = (
            "同一Wyckoff阶段内的子阶段变化需要连续确认，不能单日跳级"
        )
        data["confirmed"] = False
        phase_change = dict(data.get("phase_change") or {})
        phase_change.update({
            "from": previous_phase,
            "to": raw_phase,
            "confirmed": False,
            "invalidated_by": "",
            "denial_level": "subphase_unconfirmed",
        })
        data["phase_change"] = phase_change
        _refresh_confirmation_fields(data, structural_phase_change_confirmed=False)
        return data

    data["phase"] = raw_phase
    data["phase_state_changed"] = bool(
        previous_phase and raw_phase and previous_family != raw_family
    )
    data["phase_guard_reason"] = ""
    data["reason"] = _strip_stale_phase_guard_reason(data.get("reason", ""))
    if data["phase_state_changed"]:
        phase_change = dict(data.get("phase_change") or {})
        phase_change["from"] = previous_phase
        phase_change["to"] = raw_phase
        if phase_change.get("denial_level") in {"illegal_transition", "insufficient"}:
            phase_change["denial_level"] = ""
            phase_change["invalidated_by"] = ""
        data["phase_change"] = phase_change
    structural_confirmed = bool(
        data["phase_state_changed"]
        and _has_decisive_directional_signal(data, raw_family)
    )
    if structural_confirmed:
        phase_change = dict(data.get("phase_change") or {})
        phase_change["confirmed"] = True
        data["phase_change"] = phase_change
    _refresh_confirmation_fields(data, structural_phase_change_confirmed=structural_confirmed)
    return data
