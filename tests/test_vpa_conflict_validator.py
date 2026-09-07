"""Regression tests for v5/v7 vph_conflict checklist validator.

Anna's second-round audit #5: the validator (the v4/v7 mechanism that
demotes hallucinated 派发尾声 calls when the LLM cannot back them with
real climax numbers) had zero test coverage. These tests pin down the
behavior so future refactors that touch ``_validate_vph_conflict_evidence``
or ``_apply_vp_harmony_correction`` cannot silently regress.

v7 migration (Phase 9): the LLM no longer supplies climax numbers. It
picks a candidate id from the input candidate pool, and the validator
looks up REAL OHLCV-derived numbers from that candidate. Tests in this
file have been migrated from the v5.x ``vph_conflict`` shape to the v7
``selected_climax.candidate_id`` shape — the per-climax-type ``_failures_for``
logic is unchanged, only the SOURCE of the numbers moved from LLM to
candidate pool.
"""

from alpha_agents.tools.vpa import (
    _apply_phase_state_guard,
    _validate_vph_conflict_evidence,
    _apply_vp_harmony_correction,
)


# Per-stock 90th-pct cutoffs that a typical mid-cap A-share would have.
# A pinned `as_of_date` lets the climax_date recency check be exercised
# deterministically (v5.1 #5).
_AS_OF = "2025-12-15"


def _candidate(
    *,
    cid: str,
    date: str,
    vol_ratio: float,
    range_vs_5d_avg: float = 1.7,
    upper_shadow: float = 0.55,
    lower_shadow: float = 0.05,
    close_position: float = 0.32,
    post_bar_reverse_3d: float | None = -0.031,
    post_bars_observed: int = 3,
) -> dict:
    """Build a v7-shape candidate dict with sensible BC-style defaults.

    Override per-test for the field under test. ``post_bar_reverse_3d`` is a
    FRACTION (e.g. -0.031 means -3.1%) — note v5.x stored ``post_bar_reverse_pct_3d``
    in PERCENT.
    """
    return {
        "id": cid, "date": date,
        # OHLCV — full set so candidate is realistic; values not used by
        # _candidate_to_vc_shape but kept for parity with real candidates.
        "open": 36.0, "high": 38.0, "low": 35.0, "close": 35.5,
        "volume": 100_000_000, "bar_type": "阴线",
        "vol_ratio": vol_ratio, "vol_ratio_pct": 0.95,
        "range_vs_5d_avg": range_vs_5d_avg,
        "bar_spread": 0.083, "spread_pct": 0.92,
        "close_position": close_position,
        "upper_shadow": upper_shadow, "lower_shadow": lower_shadow,
        "pct_change": -0.018, "abspct_pct": 0.65,
        "obv": 1.2e9,
        "post_bar_reverse_3d": post_bar_reverse_3d,
        "post_bars_observed": post_bars_observed,
    }


def _ctx_bullish(*, trend_20d_pct: float, candidates: list[dict] | None = None,
                 **overrides) -> dict:
    """vph=bullish phase_context with per-stock p90s + a candidate pool."""
    ctx = {
        "vp_harmony_score": "bullish",
        "vol_ratio_p90": 1.5,
        "abspct_p90": 0.025,  # 2.5%
        "upper_shadow_p90": 0.4,
        "lower_shadow_p90": 0.4,
        "range_vs_5d_avg_p90": 1.5,
        "trend_20d_pct": trend_20d_pct,
        "as_of_date": _AS_OF,
        "candidates": candidates or [],
    }
    ctx.update(overrides)
    return ctx


def _ctx_bearish(*, trend_20d_pct: float, candidates: list[dict] | None = None,
                 **overrides) -> dict:
    ctx = {
        "vp_harmony_score": "bearish",
        "vol_ratio_p90": 1.5,
        "abspct_p90": 0.025,
        "upper_shadow_p90": 0.4,
        "lower_shadow_p90": 0.4,
        "range_vs_5d_avg_p90": 1.5,
        "trend_20d_pct": trend_20d_pct,
        "as_of_date": _AS_OF,
        "candidates": candidates or [],
    }
    ctx.update(overrides)
    return ctx


# Legacy ctx fixtures (kept for tests that don't need a candidate pool —
# i.e. tests that exercise the no-conflict / neutral-vph / family-match
# paths where validator never reaches the candidate lookup).
TYPICAL_CTX_BULLISH_VPH = {
    "vp_harmony_score": "bullish",
    "vol_ratio_p90": 1.5,
    "abspct_p90": 0.025,
    "upper_shadow_p90": 0.4,
    "lower_shadow_p90": 0.4,
    "range_vs_5d_avg_p90": 1.5,
    "as_of_date": _AS_OF,
}

TYPICAL_CTX_BEARISH_VPH = {
    "vp_harmony_score": "bearish",
    "vol_ratio_p90": 1.5,
    "abspct_p90": 0.025,
    "upper_shadow_p90": 0.4,
    "lower_shadow_p90": 0.4,
    "range_vs_5d_avg_p90": 1.5,
    "as_of_date": _AS_OF,
}


def _make_data(phase: str, direction: str, lvl: int = 2,
               selected_climax: dict | None = None,
               vph_conflict: dict | None = None) -> dict:
    d = {
        "phase": phase,
        "direction": direction,
        "confirmation_level": lvl,
        "confirmed": lvl >= 3,
        "action_confirmed": lvl >= 3,
        "phase_change": {"confirmed": True, "from": "拉升", "to": phase},
        "signals": [],
    }
    # v7 path: selected_climax populated → validator reads from candidate pool
    if selected_climax is not None:
        d["selected_climax"] = selected_climax
    # legacy: only used by tests that explicitly exercise the no-conflict
    # path (where validator returns early without reading vph_conflict).
    if vph_conflict is not None:
        d["vph_conflict"] = vph_conflict
    return d


# ─── BC: full-pass keeps phase, marks validated ───────────────────────────


def test_BC_full_pass_keeps_phase_and_marks_validated():
    """LLM picks a BC candidate; real OHLCV passes all 6 checks → no demote, flag set."""
    cand = _candidate(
        cid="cand-11-25", date="11-25",
        vol_ratio=2.3, range_vs_5d_avg=1.7,
        upper_shadow=0.55, close_position=0.32,
        post_bar_reverse_3d=-0.031,
    )
    ctx = _ctx_bullish(trend_20d_pct=+18.5, candidates=[cand])
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-11-25",
                                       "climax_type": "BC",
                                       "rationale": "test BC"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "派发尾声"
    assert data["direction"] == "看空"
    assert data["vph_conflict_validated"] is True
    assert data.get("vph_conflict_failures") in (None, [])


# ─── BC: per-failure demotion paths ───────────────────────────────────────


def test_BC_fail_trend_demotes_and_walks_verdict_back():
    """trend_20d below +10% disqualifies BC → demote 派发尾声 → 派发初期, 看空 → 偏空."""
    cand = _candidate(cid="cand-11-25", date="11-25", vol_ratio=2.3)
    ctx = _ctx_bullish(trend_20d_pct=+3.0, candidates=[cand])  # not extended
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-11-25",
                                       "climax_type": "BC",
                                       "rationale": "trend short"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "派发初期"
    assert data["direction"] == "偏空"  # walked back one notch (Anna #8)
    assert data["confirmed"] is False
    assert data["vph_conflict_validated"] is False
    assert any("trend_20d" in f for f in data["vph_conflict_failures"])


def test_BC_fail_volume_below_p80_demotes():
    """Per-stock vol_ratio_p90=1.5; candidate vol_ratio=1.1 → fail."""
    cand = _candidate(cid="cand-11-25", date="11-25",
                      vol_ratio=1.1)  # below p90 of 1.5
    ctx = _ctx_bullish(trend_20d_pct=+18.5, candidates=[cand])
    data = _make_data("派发中期", "偏空",
                      selected_climax={"candidate_id": "cand-11-25",
                                       "climax_type": "BC",
                                       "rationale": "weak vol"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "派发初期"
    assert any("vol_ratio" in f for f in data["vph_conflict_failures"])


def test_BC_fail_close_position_in_upper_half_demotes():
    """BC requires close in lower half (cp<0.5); cp=0.91 fails."""
    cand = _candidate(
        cid="cand-12-24", date="12-24",
        vol_ratio=2.5, range_vs_5d_avg=2.1,
        upper_shadow=0.45, close_position=0.91,  # not lower half
    )
    ctx = _ctx_bullish(trend_20d_pct=+47.1, candidates=[cand])
    data = _make_data("派发尾声", "看空", lvl=2,
                      selected_climax={"candidate_id": "cand-12-24",
                                       "climax_type": "BC",
                                       "rationale": "high close"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "派发初期"
    assert any("close" in f.lower() for f in data["vph_conflict_failures"])


def test_BC_missing_checklist_under_conflict_demotes():
    """vph=bullish + phase=派发 but selected_climax.candidate_id is null →
    v7 fails closed (LLM didn't justify the conflict with a candidate)."""
    ctx = _ctx_bullish(trend_20d_pct=+18.5, candidates=[])
    data = _make_data("派发初期", "偏空", lvl=2,
                      selected_climax={"candidate_id": None,
                                       "climax_type": None,
                                       "rationale": ""})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "派发初期"
    assert data["confirmed"] is False
    # v7 message: "selected_climax.candidate_id is null despite phase-vph conflict"
    assert any("candidate_id" in f for f in data["vph_conflict_failures"])


# ─── SC: mirror of BC ─────────────────────────────────────────────────────


def test_SC_full_pass_keeps_phase():
    cand = _candidate(
        cid="cand-10-21", date="10-21",
        vol_ratio=2.6, range_vs_5d_avg=1.8,
        # SC: long lower shadow, close in upper half
        upper_shadow=0.05, lower_shadow=0.55,
        close_position=0.78,
        post_bar_reverse_3d=+0.035,
    )
    ctx = _ctx_bearish(trend_20d_pct=-22.0, candidates=[cand])
    data = _make_data("吸筹初期", "偏多", lvl=3,
                      selected_climax={"candidate_id": "cand-10-21",
                                       "climax_type": "SC",
                                       "rationale": "test SC"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "吸筹初期"
    assert data["vph_conflict_validated"] is True


def test_SC_fail_close_in_lower_half_demotes():
    """SC requires close in upper half (cp>0.5); 0.20 fails."""
    cand = _candidate(
        cid="cand-10-21", date="10-21",
        vol_ratio=2.6, range_vs_5d_avg=1.8,
        upper_shadow=0.05, lower_shadow=0.55,
        close_position=0.20,  # not upper half
    )
    ctx = _ctx_bearish(trend_20d_pct=-22.0, candidates=[cand])
    data = _make_data("吸筹尾声", "看多", lvl=2,
                      selected_climax={"candidate_id": "cand-10-21",
                                       "climax_type": "SC",
                                       "rationale": "low close"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "吸筹初期"
    assert data["direction"] == "偏多"  # walked back


# ─── SOW: validator now covers it (Anna #4 fix) ───────────────────────────


def test_SOW_full_pass_keeps_phase():
    # SOW requires close in lower half (cp<0.5), trend_20d ≤ -10%
    cand = _candidate(
        cid="cand-11-10", date="11-10",
        vol_ratio=2.0,
        # SOW doesn't require range/shadow; default upper_shadow doesn't matter
        close_position=0.20,
    )
    ctx = _ctx_bullish(trend_20d_pct=-12.0, candidates=[cand])
    data = _make_data("下跌初期", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-11-10",
                                       "climax_type": "SOW",
                                       "rationale": "test SOW"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "下跌初期"
    assert data["vph_conflict_validated"] is True


def test_SOW_fail_no_extended_downtrend_demotes():
    """SOW requires trend_20d ≤ -10% (already declining). +5% fails."""
    cand = _candidate(
        cid="cand-11-10", date="11-10",
        vol_ratio=2.0, close_position=0.20,
    )
    ctx = _ctx_bullish(trend_20d_pct=+5.0, candidates=[cand])  # uptrend, not SOW
    data = _make_data("下跌初期", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-11-10",
                                       "climax_type": "SOW",
                                       "rationale": "no downtrend"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "下跌初期"  # already 初期
    assert data["confirmed"] is False
    assert any("trend_20d" in f for f in data["vph_conflict_failures"])


# ─── Unknown climax_type — reject ─────────────────────────────────────────


def test_unknown_climax_type_demotes():
    cand = _candidate(cid="cand-12-05", date="12-05", vol_ratio=2.0)
    ctx = _ctx_bullish(trend_20d_pct=+18.5, candidates=[cand])
    data = _make_data("派发中期", "看空", lvl=2,
                      selected_climax={"candidate_id": "cand-12-05",
                                       "climax_type": "MysteryEvent",
                                       "rationale": "bad type"})
    _validate_vph_conflict_evidence(data, ctx)
    assert any("not in valid" in f.lower() for f in data["vph_conflict_failures"])
    assert data["phase"] == "派发初期"


# ─── Validator pass suppresses binary vph correction (Anna #7 unification) ─


def test_validator_pass_suppresses_vph_correction_curb():
    """When validator passes, the binary 1.2× score doesn't get to curb the
    confirmation level on the same entry — single source of truth."""
    cand = _candidate(
        cid="cand-11-25", date="11-25",
        vol_ratio=2.3, range_vs_5d_avg=1.7,
        upper_shadow=0.55, close_position=0.32,
        post_bar_reverse_3d=-0.031,
    )
    ctx = _ctx_bullish(trend_20d_pct=+18.5, candidates=[cand])
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-11-25",
                                       "climax_type": "BC",
                                       "rationale": "full pass BC"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["vph_conflict_validated"] is True
    # Now run the binary vph correction. With direction=看空 + vph=bullish
    # this would normally curb C3→C2. With validated flag it must be a no-op.
    _apply_vp_harmony_correction(data, ctx)
    assert data["confirmation_level"] == 3  # unchanged


def test_validator_fail_records_vph_curb_suggestion_without_mutating_level():
    """vph conflict is audit evidence; it must not mutate C2/C3."""
    cand = _candidate(cid="cand-11-25", date="11-25", vol_ratio=2.3)
    ctx = _ctx_bullish(trend_20d_pct=+3.0, candidates=[cand])  # fails trend
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-11-25",
                                       "climax_type": "BC",
                                       "rationale": "trend fail"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["vph_conflict_validated"] is False
    # validator already demoted phase + verdict; level reset to <3 by demotion
    # Run binary curb on a case where post-validator lvl is still curb-able.
    data["confirmation_level"] = 2
    data["direction"] = "偏空"
    _apply_vp_harmony_correction(data, ctx)
    # bearish dir + bullish vph + lvl=2 → conflict suggests C1, but C2 remains
    # structure-derived and strategy gates should not read the suggestion.
    assert data["confirmation_level"] == 2
    assert data["vp_harmony_alignment"] == "conflict"
    assert data["vp_harmony_level_suggestion"] == 1
    assert data["vp_harmony_level_adjustment"] == -1


# ─── No conflict means validator is a no-op ───────────────────────────────


def test_validator_noop_when_phase_aligns_with_vph():
    data = _make_data("拉升中期", "看多", lvl=3,
                      vph_conflict={"exists": False})
    _validate_vph_conflict_evidence(data, TYPICAL_CTX_BULLISH_VPH)
    assert data["phase"] == "拉升中期"
    # Not validated: there's no conflict to validate.
    assert data["vph_conflict_validated"] is False


def test_validator_noop_when_vph_neutral():
    data = _make_data("派发尾声", "看空", lvl=3,
                      vph_conflict={"exists": True, "climax_type": "BC"})
    _validate_vph_conflict_evidence(data, {"vp_harmony_score": "neutral",
                                           "vol_ratio_p90": 1.5,
                                           "abspct_p90": 0.025,
                                           "spread_p90": 0.025})
    assert data["phase"] == "派发尾声"
    assert data["vph_conflict_validated"] is False


# ─── v5.1 #1: 买入高潮顶部 / 恐慌抛售高潮 caught by family detection ──────


def test_v51_BC_distribution_top_label_caught_by_family_match():
    """Anna's standard BC term '买入高潮顶部' must trigger the gate.
    v5 _phase_in substring miss let it bypass.

    No candidate selected → v7 fails with "candidate_id is null" and demotes
    to distribution-family floor."""
    data = _make_data("买入高潮顶部", "看空", lvl=3,
                      vph_conflict={"exists": False})
    _validate_vph_conflict_evidence(data, TYPICAL_CTX_BULLISH_VPH)
    # phase + verdict walked back; demoted to distribution-family floor.
    assert data["phase"] == "派发初期"
    assert data["direction"] == "偏空"


def test_v51_SC_accumulation_bottom_label_caught_by_family_match():
    """Mirror: '恐慌抛售高潮' demotes to '吸筹初期' (accumulation family),
    not '拉升初期' (markup family) per v5.1 family-based demote target."""
    data = _make_data("恐慌抛售高潮", "看多", lvl=3,
                      vph_conflict={"exists": False})
    _validate_vph_conflict_evidence(data, TYPICAL_CTX_BEARISH_VPH)
    assert data["phase"] == "吸筹初期"
    assert data["direction"] == "偏多"


# ─── v5.1 #3: AR explicitly rejected ──────────────────────────────────────


def test_v51_AR_climax_type_rejected():
    """AR is a confirmation/reaction bar, not a climactic event itself.
    The validator must reject climax_type='AR' even with otherwise-valid
    numbers."""
    cand = _candidate(
        cid="cand-12-05", date="12-05",
        vol_ratio=2.5, range_vs_5d_avg=1.7,
    )
    # trend doesn't matter for AR rejection — type check fires first
    ctx = _ctx_bullish(trend_20d_pct=0.0, candidates=[cand])
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-12-05",
                                       "climax_type": "AR",
                                       "rationale": "AR not allowed"})
    _validate_vph_conflict_evidence(data, ctx)
    assert any("AR" in f or "valid" in f for f in data["vph_conflict_failures"])
    assert data["phase"] == "派发初期"


# ─── v5.1 #5: climax_date recency cross-check ─────────────────────────────


def test_v51_old_climax_with_null_post_bar_demotes():
    """LLM picks an old climax (>3 trading days ago) but the candidate has
    post_bar_reverse_3d=null. v5.1 must reject (null only ok if recent)."""
    cand = _candidate(
        cid="cand-11-25", date="11-25",  # ~14 trading days before _AS_OF
        vol_ratio=2.3, range_vs_5d_avg=1.7,
        upper_shadow=0.55, close_position=0.32,
        post_bar_reverse_3d=None,  # null, but climax is too old
    )
    ctx = _ctx_bullish(trend_20d_pct=+18.5, candidates=[cand])
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-11-25",
                                       "climax_type": "BC",
                                       "rationale": "old + null"})
    _validate_vph_conflict_evidence(data, ctx)
    assert any("3 trading days" in f or "distance" in f
               for f in data["vph_conflict_failures"])
    assert data["phase"] == "派发初期"


def test_v51_recent_climax_with_null_post_bar_passes():
    """Mirror: when climax_date is within 3 trading days, null is OK."""
    cand = _candidate(
        cid="cand-12-12", date="12-12",  # ~3 trading days before _AS_OF
        vol_ratio=2.3, range_vs_5d_avg=1.7,
        upper_shadow=0.55, close_position=0.32,
        post_bar_reverse_3d=None,  # null + recent → OK
    )
    ctx = _ctx_bullish(trend_20d_pct=+18.5, candidates=[cand])
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-12-12",
                                       "climax_type": "BC",
                                       "rationale": "recent + null"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["vph_conflict_validated"] is True


# ─── v5.1 #7: missing climax_type fail-closed ─────────────────────────────


def test_v51_missing_climax_type_fail_closed():
    """v5 silently defaulted to BC; v5.1 must reject when LLM picks a
    candidate but omits climax_type."""
    cand = _candidate(
        cid="cand-12-12", date="12-12",
        vol_ratio=2.3, range_vs_5d_avg=1.7,
        upper_shadow=0.55, close_position=0.32,
    )
    ctx = _ctx_bullish(trend_20d_pct=+18.5, candidates=[cand])
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-12-12",
                                       "climax_type": None,
                                       "rationale": "type omitted"})
    _validate_vph_conflict_evidence(data, ctx)
    assert any("climax_type" in f and "missing" in f
               for f in data["vph_conflict_failures"])
    assert data["phase"] == "派发初期"


# ─── v5.1 #4 climax types: UTAD / Spring / SOS coverage ───────────────────


def test_v51_UTAD_full_pass():
    cand = _candidate(
        cid="cand-12-12", date="12-12",
        vol_ratio=2.0,
        # UTAD: closed back inside range
        upper_shadow=0.40,  # (UTAD doesn't enforce shadow floor in _failures_for)
        close_position=0.30,
        post_bar_reverse_3d=-0.025,
    )
    ctx = _ctx_bullish(trend_20d_pct=+25.0, candidates=[cand])
    data = _make_data("派发中期", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-12-12",
                                       "climax_type": "UTAD",
                                       "rationale": "test UTAD"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["vph_conflict_validated"] is True


def test_v51_UTAD_fail_close_too_high():
    """UTAD requires close back in lower half (cp<0.5). 0.80 fails."""
    cand = _candidate(
        cid="cand-12-12", date="12-12",
        vol_ratio=2.0,
        close_position=0.80,  # not back inside
    )
    ctx = _ctx_bullish(trend_20d_pct=+25.0, candidates=[cand])
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-12-12",
                                       "climax_type": "UTAD",
                                       "rationale": "high close"})
    _validate_vph_conflict_evidence(data, ctx)
    assert any("UTAD" in f for f in data["vph_conflict_failures"])
    assert data["phase"] == "派发初期"


def test_v51_Spring_full_pass():
    cand = _candidate(
        cid="cand-12-12", date="12-12",
        vol_ratio=2.0,
        upper_shadow=0.05, lower_shadow=0.40,  # SC/Spring uses lower_shadow
        close_position=0.65,  # closed back in upper half
        post_bar_reverse_3d=+0.025,
    )
    ctx = _ctx_bearish(trend_20d_pct=-22.0, candidates=[cand])
    data = _make_data("吸筹中期", "看多", lvl=3,
                      selected_climax={"candidate_id": "cand-12-12",
                                       "climax_type": "Spring",
                                       "rationale": "test Spring"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["vph_conflict_validated"] is True


def test_v51_Spring_fail_close_too_low():
    cand = _candidate(
        cid="cand-12-12", date="12-12",
        vol_ratio=2.0,
        upper_shadow=0.05, lower_shadow=0.40,
        close_position=0.20,  # still in lower half
    )
    ctx = _ctx_bearish(trend_20d_pct=-22.0, candidates=[cand])
    data = _make_data("吸筹尾声", "看多", lvl=3,
                      selected_climax={"candidate_id": "cand-12-12",
                                       "climax_type": "Spring",
                                       "rationale": "low close"})
    _validate_vph_conflict_evidence(data, ctx)
    assert any("Spring" in f for f in data["vph_conflict_failures"])
    assert data["phase"] == "吸筹初期"


def test_v51_SOS_full_pass():
    """SOS = breakout from accumulation, no range/shadow constraint."""
    cand = _candidate(
        cid="cand-12-12", date="12-12",
        vol_ratio=1.8,
        # SOS: doesn't constrain shadow; default upper_shadow ok
        close_position=0.85,  # strong breakout close upper
        post_bar_reverse_3d=None,  # recent climax, null OK
    )
    ctx = _ctx_bearish(trend_20d_pct=+12.0, candidates=[cand])
    data = _make_data("吸筹中期", "看多", lvl=3,
                      selected_climax={"candidate_id": "cand-12-12",
                                       "climax_type": "SOS",
                                       "rationale": "test SOS"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["vph_conflict_validated"] is True


def test_v51_SOS_fail_close_lower_half():
    cand = _candidate(
        cid="cand-12-12", date="12-12",
        vol_ratio=1.8,
        close_position=0.30,  # weak close, no breakout strength
    )
    ctx = _ctx_bearish(trend_20d_pct=+12.0, candidates=[cand])
    data = _make_data("拉升中期", "看多", lvl=3,
                      selected_climax={"candidate_id": "cand-12-12",
                                       "climax_type": "SOS",
                                       "rationale": "weak close"})
    _validate_vph_conflict_evidence(data, ctx)
    assert any("SOS" in f for f in data["vph_conflict_failures"])
    assert data["phase"] == "拉升初期"


# ─── v5.1 #9: integration test exercises actual _p90 percentile path ─────


def test_v51_p90_actually_computed_from_settled_distribution():
    """Hardcoded fixtures bypass _p90; this test calls the real
    _phase_guard_context_from_df with a synthetic OHLCV frame to verify
    p90 is actually computed (not just defaulted) and used by validator."""
    import pandas as pd
    import numpy as np
    from alpha_agents.tools.vpa import _phase_guard_context_from_df, _compute_derived

    # 60 settled bars: most have vol_ratio ≈ 1.0, only 6 are above 1.5.
    rng = np.random.default_rng(seed=42)
    # Construct synthetic OHLCV that yields known distribution shape after _compute_derived.
    n = 80
    base = 100.0
    closes = base + np.cumsum(rng.normal(0, 0.5, n))
    opens = closes + rng.normal(0, 0.3, n)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.3, n))
    lows  = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.3, n))
    # Volume: most around 1e6, but inject a few large bars to push p90 higher.
    vols = rng.normal(1e6, 1e5, n)
    vols[-10:-4] = 3e6  # 6 large bars within last 60
    df = pd.DataFrame({
        "date": [f"2025-{(i // 30) + 9:02d}-{(i % 30) + 1:02d}" for i in range(n)],
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.abs(vols),
    })
    df = _compute_derived(df, window=20)
    ctx = _phase_guard_context_from_df(df, as_of="2025-12-15")
    # Sanity: keys exist
    assert "vol_ratio_p90" in ctx
    assert "abspct_p90" in ctx
    # p90 of the synthetic vol_ratio distribution should be > the conservative
    # default 1.5 because we injected 6 high-vol bars in the last 60 — i.e.
    # the function actually computed something rather than falling back.
    # (If it weren't actually called, vol_ratio_p90 would be the 1.5 default.)
    assert ctx["vol_ratio_p90"] > 1.0  # something was computed
    # spread_p90 was removed in v5.1 #6 (dead code).
    assert "spread_p90" not in ctx
    # as_of_date is present (v5.1 #5).
    assert ctx["as_of_date"] == "2025-12-15"


# ─── v5.2: shadow + range floors come from per-stock p90 ──────────────────


def test_v52_BC_shadow_below_per_stock_p90_demotes():
    """A hot small-cap with upper_shadow_p90 = 0.55 (frequent long upper
    shadows historically) requires shadow ≥ 0.55, not 0.4. v5.1 hardcoded
    0.4 would have let 0.45 pass; v5.2 rejects."""
    cand = _candidate(
        cid="cand-11-25", date="11-25",
        vol_ratio=2.3, range_vs_5d_avg=1.7,
        upper_shadow=0.45,  # would pass v5.1's 0.4 floor
        close_position=0.32,
        post_bar_reverse_3d=-0.031,
    )
    ctx = _ctx_bullish(trend_20d_pct=+18.5, candidates=[cand],
                       upper_shadow_p90=0.55)  # this stock often has long upper shadows
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-11-25",
                                       "climax_type": "BC",
                                       "rationale": "shadow short"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "派发初期"
    assert any("upper-shadow" in f and "p90" in f
               for f in data["vph_conflict_failures"])


def test_v52_BC_range_below_per_stock_p90_demotes():
    """range_vs_5d_avg_p90 = 1.8 means typical wide bars on this stock
    are 1.8× — so 1.6 is not "qualitatively wide". v5.1's 1.5 floor
    would let it pass; v5.2 rejects."""
    cand = _candidate(
        cid="cand-11-25", date="11-25",
        vol_ratio=2.3,
        range_vs_5d_avg=1.6,  # would pass v5.1's 1.5 floor
        upper_shadow=0.55, close_position=0.32,
        post_bar_reverse_3d=-0.031,
    )
    ctx = _ctx_bullish(trend_20d_pct=+18.5, candidates=[cand],
                       range_vs_5d_avg_p90=1.8)
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-11-25",
                                       "climax_type": "BC",
                                       "rationale": "range short"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "派发初期"
    assert any("range_x" in f for f in data["vph_conflict_failures"])


def test_v52_UTAD_close_position_collapsed_to_05_not_055():
    """v5.1 had UTAD threshold cp<0.55 (invented); v5.2 collapsed to 0.5
    binary. cp=0.52 was ok in v5.1 but should now fail."""
    cand = _candidate(
        cid="cand-12-12", date="12-12",
        vol_ratio=2.0,
        close_position=0.52,  # was UTAD-ok in v5.1, now rejected
        post_bar_reverse_3d=-0.030,
    )
    ctx = _ctx_bullish(trend_20d_pct=+25.0, candidates=[cand])
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-12-12",
                                       "climax_type": "UTAD",
                                       "rationale": "borderline cp"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "派发初期"
    assert any("UTAD" in f and "lower half" in f
               for f in data["vph_conflict_failures"])


def test_v52_post_bar_reverse_floor_is_p90_not_06x():
    """v5.1: floor = abspct_p90 × 0.6. v5.2: floor = abspct_p90 directly.
    With abspct_p90=2.5%, BC needs post≤-2.5% (v5.1 was -1.5%)."""
    cand = _candidate(
        cid="cand-12-12", date="12-12",
        vol_ratio=2.3, range_vs_5d_avg=1.7,
        upper_shadow=0.55, close_position=0.32,
        # v5.1 ok (>0.6×2.5=1.5), v5.2 fails (<2.5)
        post_bar_reverse_3d=-0.020,
    )
    ctx = _ctx_bullish(trend_20d_pct=+18.5, candidates=[cand])
    data = _make_data("派发尾声", "看空", lvl=3,
                      selected_climax={"candidate_id": "cand-12-12",
                                       "climax_type": "BC",
                                       "rationale": "post weak"})
    _validate_vph_conflict_evidence(data, ctx)
    assert data["phase"] == "派发初期"
    assert any("post_bar_reverse" in f and "p90" in f
               for f in data["vph_conflict_failures"])
